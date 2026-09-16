"""The eval CLI: run the golden set, print a report, fail the build on a regression.

    python -m evals.run                                  # every case, against the baseline
    python -m evals.run --tag plastics --limit 10
    python -m evals.run --model gpt-5.6-luna --json luna.json
    python -m evals.run --update-baseline                # record, do not compare
    python -m evals.run --dry-run                        # parse the cases; no API, no DB

Exit codes — this is a gate, so they are the contract:

    0   ran, and nothing regressed beyond the tolerance
    1   a regression (see `compare_to_baseline`: groundedness below 1.0 always, or any gated
        metric worse by more than one case's worth of rate)
    2   could not run at all: no cases, no API key, a malformed golden file, a dead database

`--model` is one flag because "terra or luna" is the open question the architecture flagged and
it should cost one command to answer: the override goes into the environment BEFORE the first
`get_settings()` result is used, so the model id reaching the API, the price table and the
recorded `classification` row are all the same string. One model per process — `build_agent`
is cached per prompt, so a second model in the same run would answer from the first one's agent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from evals.scoring import (
    Case,
    CaseError,
    Observation,
    Report,
    ReportMeta,
    baseline_from_report,
    build_report,
    compare_to_baseline,
    filter_cases,
    load_baseline,
    load_cases,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "golden"
DEFAULT_BASELINE = ROOT / "evals" / "baseline.json"

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_SETUP = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run",
        description="Run the UKTZED golden set through the real classifier and score it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--cases",
        default=str(DEFAULT_CASES),
        help="golden case file or directory (default: evals/golden)",
    )
    parser.add_argument("--tag", default=None, help="run only cases carrying this tag")
    parser.add_argument("--limit", type=int, default=None, help="first N cases after --tag")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="cases in flight at once (default: 4)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="override settings.model for this run, e.g. gpt-5.6-luna",
    )
    parser.add_argument("--user-id", type=int, default=1, help="owner of the recorded rows")
    parser.add_argument(
        "--baseline",
        default=str(DEFAULT_BASELINE),
        help="baseline file to compare against (default: evals/baseline.json)",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="write this run's numbers to --baseline instead of gating on them",
    )
    parser.add_argument("--json", dest="json_out", default=None, help="write the full report here")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="load and validate the cases, print them, and exit without calling anything",
    )
    parser.add_argument(
        "--max-failures",
        type=int,
        default=20,
        help="how many failing cases to print in full (default: 20)",
    )
    parser.add_argument("--verbose", action="store_true", help="show the app's INFO logs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        cases = filter_cases(load_cases(args.cases), tag=args.tag, limit=args.limit)
    except CaseError as exc:
        print(f"golden set: {exc}", file=sys.stderr)
        return EXIT_SETUP

    if not cases:
        where = f"{args.cases}" + (f" with tag {args.tag!r}" if args.tag else "")
        print(
            f"no cases found in {where}. evals/golden/ holds the golden set; see "
            "`evals.scoring.Case` for the file format.",
            file=sys.stderr,
        )
        return EXIT_SETUP

    if args.dry_run:
        _print_cases(cases)
        return EXIT_OK

    # The override has to land before anything reads Settings for real. `app.agent.agent`
    # already called `get_settings()` at import time (to hand the API key to the SDK), so the
    # cache is warm and must be dropped, not merely written over.
    if args.model:
        os.environ["MODEL"] = args.model
    from app.settings import get_settings  # imported late, so --dry-run needs no settings

    get_settings.cache_clear()
    settings = get_settings()
    if not (settings.openai_api_key or os.environ.get("OPENAI_API_KEY")):
        print(
            "OPENAI_API_KEY is not set — this runs the real agent against the live API.",
            file=sys.stderr,
        )
        return EXIT_SETUP

    try:
        report = asyncio.run(_run(cases, args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_SETUP
    except Exception as exc:  # setup failed: no database, no dataset, no pool
        print(f"eval run failed to start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_SETUP

    print(report.render(max_failures=args.max_failures))

    if args.json_out:
        Path(args.json_out).write_text(report.to_json(), encoding="utf-8")
        print(f"wrote {args.json_out}")

    if args.update_baseline:
        _write_baseline(Path(args.baseline), report)
        print(
            f"\nbaseline updated: {args.baseline}\n"
            "Commit it together with the change that moved the numbers, and say why."
        )
        return EXIT_OK

    try:
        baseline = load_baseline(args.baseline)
    except CaseError as exc:
        print(f"baseline: {exc}", file=sys.stderr)
        return EXIT_SETUP

    comparison = compare_to_baseline(report, baseline)
    print()
    print(comparison.render())
    return EXIT_OK if comparison.ok else EXIT_REGRESSION


async def _run(cases: list[Case], args: argparse.Namespace) -> Report:
    # Imported here, not at module import: `--dry-run` and `--help` must work with no
    # database, no API key and no Agents SDK configuration.
    from evals.harness import HarnessOptions, open_harness

    options = HarnessOptions(
        concurrency=max(1, args.concurrency),
        user_id=args.user_id,
    )
    started = time.perf_counter()
    done = 0
    total = len(cases)

    def progress(case: Case, observation: Observation) -> None:
        nonlocal done
        done += 1
        marker = observation.error_class or observation.outcome
        code = observation.primary_code or "—"
        print(
            f"[{done:>3}/{total}] {case.id:<28} {marker:<14} {code:<12} "
            f"{observation.wall_ms / 1000:5.1f}s",
            file=sys.stderr,
            flush=True,
        )

    async with open_harness(options) as harness:
        meta = await harness.meta()
        print(
            f"running {total} case(s) · model {meta.model} · prompt {meta.prompt_version} · "
            f"concurrency {options.concurrency}",
            file=sys.stderr,
            flush=True,
        )
        observations = await harness.run_many(cases, on_done=progress)

    elapsed = time.perf_counter() - started
    print(f"{total} case(s) in {elapsed:.1f}s wall clock", file=sys.stderr)
    return build_report(
        zip(cases, observations, strict=True),
        ReportMeta(
            model=meta.model,
            prompt_version=meta.prompt_version,
            prompt_sha256=meta.prompt_sha256,
            dataset_sha256=meta.dataset_sha256,
            cases_path=str(args.cases),
            tag=args.tag,
            concurrency=options.concurrency,
        ),
    )


def _write_baseline(path: Path, report: Report) -> None:
    payload: dict[str, Any] = baseline_from_report(report)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _print_cases(cases: list[Case]) -> None:
    """`--dry-run`: proves the golden set parses, with no API key and no database."""
    print(f"{len(cases)} case(s)")
    for case in cases:
        expectation = case.expect_code or (
            f"{case.expect_heading}……" if case.expect_heading else case.expect_outcome
        )
        tags = ",".join(case.tags) or "-"
        print(
            f"  {case.id:<28} {case.expect_outcome:<14} {expectation:<12} {tags:<18} "
            f"{case.input[:60]}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
