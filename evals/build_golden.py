#!/usr/bin/env python3
"""Verify every golden case against the real tariff. Offline: no database, no API.

    python3 evals/build_golden.py

This is the reason the golden set is worth anything. A golden set whose ground truth was
invented is worse than no golden set at all: it reports a number, the number looks like
accuracy, and it measures nothing. So no `expect_code` is trusted because it was written
down — each one is looked up in `data/uktzed_hierarchical.json` and has to come back
existing AND terminal, `accept_codes` likewise, and each `expect_heading` has to be a real
4-digit category.

The lookup runs through `app.tariff.ingest.prepare`, the same function the Postgres ingest
uses, so "terminal" here means exactly what it will mean in the database: *no other code in
this category has me as a strict prefix*, authored at ingest and never spelled
``len(code) == 10``. Reimplementing the rule in this file would let the two definitions
drift, and the drift would be silent.

Every violation is collected and printed; the script does not stop at the first one and it
exits non-zero if there is any. Warnings (a residual "інші" leaf, a leaf whose `full_path`
is shared with a sibling) are printed but do not fail the run — they are a reviewer's cue,
not a defect.

The loader is importable: the eval harness calls `load_cases()` rather than re-parsing the
YAML itself, so the schema has exactly one implementation.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.tariff.ingest import IngestNode, prepare  # noqa: E402

DEFAULT_GOLDEN_DIR = ROOT / "evals" / "golden"
DEFAULT_TARIFF = ROOT / "data" / "uktzed_hierarchical.json"

OUTCOMES: tuple[str, ...] = ("result", "clarification", "conversation")
"""How a turn ends, decided by which terminal tool the agent called.

`clarification` is NOT a failure — in v1 production 17.4% of turns ended this way and the
loop is part of the product. A scorer that punishes it optimises toward confident wrong
codes, which is the worst outcome a customs classifier can have.
"""

SOURCES: tuple[str, ...] = (
    "verified-live",
    "tariff-derived",
    "v1-prompt-example",
    "domain-reasoning",
)
"""Where the CASE came from. Not where its code came from — every code is verified here."""

REQUIRED_FIELDS: tuple[str, ...] = ("id", "input", "expect_outcome", "rationale", "source", "tags")
OPTIONAL_FIELDS: tuple[str, ...] = ("expect_code", "expect_heading", "accept_codes")
KNOWN_FIELDS = frozenset(REQUIRED_FIELDS + OPTIONAL_FIELDS)

ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TAG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MIN_RATIONALE_CHARS = 40
"""A rationale shorter than this is a label, not a justification."""

RESIDUAL_DESCRIPTIONS = frozenset({"інші", "інша", "інше", "інший", "інших"})
"""2,473 of 10,490 leaves are literally "інші". A human could not classify into one from a
product description either, so a case that expects one gets a warning."""


class GoldenSetError(Exception):
    """The golden set could not even be loaded. Every failure is listed, not just the first."""

    def __init__(self, failures: list[str]) -> None:
        super().__init__("golden set failed to load:\n  - " + "\n  - ".join(failures))
        self.failures = failures


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """One case. `source_file` is the file it was read from, for error messages."""

    id: str
    input: str
    expect_outcome: str
    rationale: str
    source: str
    tags: tuple[str, ...]
    source_file: str
    expect_code: str | None = None
    expect_heading: str | None = None
    accept_codes: tuple[str, ...] = ()

    @property
    def all_codes(self) -> tuple[str, ...]:
        """Every 10-digit code this case names, expected first."""
        return (self.expect_code, *self.accept_codes) if self.expect_code else self.accept_codes


@dataclass(slots=True)
class TariffIndex:
    """Lookup tables over one parsed tariff file."""

    sha256: str
    nodes: list[IngestNode]
    codes: dict[str, IngestNode] = field(default_factory=dict)
    categories: dict[str, IngestNode] = field(default_factory=dict)
    sections: dict[str, IngestNode] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> TariffIndex:
        digest, nodes = prepare(path)
        index = cls(sha256=digest, nodes=nodes)
        for node in nodes:
            if node.level == "code":
                index.codes[node.code] = node
            elif node.level == "category":
                index.categories[node.code] = node
            elif node.level == "section":
                index.sections[node.code] = node
        return index

    @property
    def terminal_count(self) -> int:
        return sum(1 for n in self.nodes if n.is_terminal)

    def section_of(self, code: str) -> str | None:
        """Section code owning a 4- or 10-digit code, or None if the code is unknown."""
        node = self.codes.get(code) or self.categories.get(code)
        if node is None or not node.ancestor_codes:
            return None
        return node.ancestor_codes[0]


@dataclass(slots=True)
class Report:
    """What the verification found. `ok` is the exit status."""

    index: TariffIndex
    cases: list[GoldenCase]
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


# --------------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------------


def _as_str_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    if any(not isinstance(item, str) for item in value):
        return None
    return value


def load_cases(golden_dir: Path = DEFAULT_GOLDEN_DIR) -> list[GoldenCase]:
    """Parse every `*.yaml` under `golden_dir` into `GoldenCase`s, in filename order.

    Raises `GoldenSetError` listing every structural problem. Field *values* are not checked
    here — that is `verify()`, which needs the tariff.
    """
    files = sorted(golden_dir.glob("*.yaml"))
    failures: list[str] = []
    cases: list[GoldenCase] = []

    if not files:
        raise GoldenSetError([f"no *.yaml files in {golden_dir}"])

    for path in files:
        name = path.name
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            failures.append(f"{name}: not valid YAML ({exc.__class__.__name__})")
            continue

        if not isinstance(payload, list) or not payload:
            failures.append(f"{name}: top level must be a non-empty list of cases")
            continue

        for position, raw in enumerate(payload, start=1):
            where = f"{name}[{position}]"
            if not isinstance(raw, dict):
                failures.append(f"{where}: case must be a mapping, got {type(raw).__name__}")
                continue

            unknown = sorted(set(raw) - KNOWN_FIELDS)
            if unknown:
                failures.append(f"{where}: unknown field(s) {unknown}")
                continue

            missing = [f for f in REQUIRED_FIELDS if f not in raw]
            if missing:
                failures.append(f"{where}: missing required field(s) {missing}")
                continue

            scalars = ("id", "input", "expect_outcome", "rationale", "source")
            bad_type = [f for f in scalars if not isinstance(raw[f], str)]
            for optional in ("expect_code", "expect_heading"):
                if optional in raw and not isinstance(raw[optional], str):
                    bad_type.append(optional)
            if bad_type:
                failures.append(f"{where}: field(s) {sorted(bad_type)} must be strings")
                continue

            tags = _as_str_list(raw["tags"])
            if tags is None:
                failures.append(f"{where}: 'tags' must be a non-empty list of strings")
                continue

            accept_raw = raw.get("accept_codes")
            if accept_raw is not None:
                accept = _as_str_list(accept_raw)
                if accept is None:
                    failures.append(
                        f"{where}: 'accept_codes' must be a non-empty list of strings "
                        f"(omit the field instead of writing an empty list)"
                    )
                    continue
            else:
                accept = []

            cases.append(
                GoldenCase(
                    id=raw["id"],
                    input=raw["input"],
                    expect_outcome=raw["expect_outcome"],
                    rationale=raw["rationale"],
                    source=raw["source"],
                    tags=tuple(tags),
                    source_file=name,
                    expect_code=raw.get("expect_code"),
                    expect_heading=raw.get("expect_heading"),
                    accept_codes=tuple(accept),
                )
            )

    if failures:
        raise GoldenSetError(failures)
    return cases


# --------------------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------------------


def _check_code(
    report: Report, case: GoldenCase, label: str, code: str, seen_codes: dict[str, str]
) -> None:
    """One 10-digit code: shape, existence, terminality. Warns on residual/ambiguous leaves."""
    where = f"{case.id} ({case.source_file})"
    index = report.index
    if not re.fullmatch(r"\d{10}", code):
        report.failures.append(f"{where}: {label} {code!r} is not 10 digits")
        return

    node = index.codes.get(code)
    if node is None:
        report.failures.append(f"{where}: {label} {code} is not in the tariff")
        return
    if not node.is_terminal:
        report.failures.append(
            f"{where}: {label} {code} exists but is NOT terminal "
            f"({node.child_count} child code(s)) — it cannot be declared"
        )
        return

    if node.description.strip().lower() in RESIDUAL_DESCRIPTIONS:
        report.warnings.append(
            f"{where}: {label} {code} is a residual leaf ({node.description!r}); "
            f"a human could not reach it from a product description either"
        )
    if node.path_is_ambiguous:
        report.warnings.append(
            f"{where}: {label} {code} shares its full_path with a sibling "
            f"(path_is_ambiguous) — the dataset cannot separate them by text"
        )
    previous = seen_codes.get(code)
    if previous is not None and previous != case.id:
        report.warnings.append(f"{where}: code {code} is also expected by {previous}")
    seen_codes.setdefault(code, case.id)


def verify(cases: list[GoldenCase], index: TariffIndex) -> Report:
    """Check every case against the tariff. Collects all failures; never raises."""
    report = Report(index=index, cases=cases)
    seen_ids: dict[str, str] = {}
    seen_codes: dict[str, str] = {}

    for case in cases:
        where = f"{case.id} ({case.source_file})"

        if not ID_PATTERN.fullmatch(case.id):
            report.failures.append(f"{where}: id must be lowercase kebab-case")
        elif case.id in seen_ids:
            report.failures.append(f"{where}: duplicate id, first seen in {seen_ids[case.id]}")
        seen_ids.setdefault(case.id, case.source_file)

        if not case.input.strip():
            report.failures.append(f"{where}: 'input' is empty")
        if case.expect_outcome not in OUTCOMES:
            report.failures.append(
                f"{where}: expect_outcome {case.expect_outcome!r} not in {list(OUTCOMES)}"
            )
        if case.source not in SOURCES:
            report.failures.append(f"{where}: source {case.source!r} not in {list(SOURCES)}")
        if len(case.rationale.strip()) < MIN_RATIONALE_CHARS:
            report.failures.append(
                f"{where}: rationale is {len(case.rationale.strip())} chars, at least "
                f"{MIN_RATIONALE_CHARS} required — say why this code and not its neighbours"
            )
        bad_tags = [t for t in case.tags if not TAG_PATTERN.fullmatch(t)]
        if bad_tags:
            report.failures.append(f"{where}: tag(s) {bad_tags} must be lowercase kebab-case")
        if len(set(case.tags)) != len(case.tags):
            report.failures.append(f"{where}: duplicate tag(s) in {list(case.tags)}")

        is_result = case.expect_outcome == "result"
        if not is_result:
            offending = [
                name
                for name, value in (
                    ("expect_code", case.expect_code),
                    ("expect_heading", case.expect_heading),
                    ("accept_codes", case.accept_codes or None),
                )
                if value
            ]
            if offending:
                report.failures.append(
                    f"{where}: expect_outcome is {case.expect_outcome!r}, so {offending} "
                    f"must be omitted — no codes are issued on that path"
                )
            continue

        if case.expect_code and case.expect_heading:
            report.failures.append(
                f"{where}: expect_code and expect_heading are mutually exclusive — "
                f"use expect_heading only where a 10-digit answer would be a coin flip"
            )
        elif not case.expect_code and not case.expect_heading:
            report.failures.append(
                f"{where}: expect_outcome is 'result' but neither expect_code nor "
                f"expect_heading is set — the case asserts nothing about the code"
            )

        if case.expect_code:
            _check_code(report, case, "expect_code", case.expect_code, seen_codes)

        if case.expect_heading:
            heading = case.expect_heading
            if not re.fullmatch(r"\d{4}", heading):
                report.failures.append(
                    f"{where}: expect_heading {heading!r} is not 4 digits"
                )
            elif heading not in index.categories:
                report.failures.append(
                    f"{where}: expect_heading {heading} is not a category in the tariff"
                )

        if case.accept_codes and not case.expect_code:
            report.failures.append(
                f"{where}: accept_codes needs an expect_code to be an alternative TO"
            )
        for code in case.accept_codes:
            if code == case.expect_code:
                report.failures.append(f"{where}: accept_codes repeats expect_code {code}")
                continue
            _check_code(report, case, "accept_codes entry", code, seen_codes)
        if len(set(case.accept_codes)) != len(case.accept_codes):
            report.failures.append(f"{where}: duplicate entries in accept_codes")

    return report


# --------------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------------


def _bar(count: int, peak: int, width: int = 24) -> str:
    """Scaled against the tallest bar in its own histogram, so small sets stay readable."""
    if peak <= 0 or count <= 0:
        return ""
    return "#" * max(1, round(width * count / peak))


def _histogram(title: str, counts: Counter[str], order: list[str] | None = None) -> list[str]:
    keys = order if order is not None else sorted(counts)
    peak = max((counts.get(k, 0) for k in keys), default=0)
    lines = [title]
    width = max((len(k) for k in keys), default=0)
    for key in keys:
        count = counts.get(key, 0)
        lines.append(f"  {key.ljust(width)}  {count:>3}  {_bar(count, peak)}")
    return lines


def format_report(report: Report, tariff_path: Path) -> str:
    """The human-readable verification report. Paste it into evals/README.md."""
    index = report.index
    cases = report.cases
    total = len(cases)
    lines: list[str] = []

    lines.append("UKTZED golden set — verification report")
    lines.append("=" * 72)
    shown = tariff_path.relative_to(ROOT) if tariff_path.is_relative_to(ROOT) else tariff_path
    lines.append(f"tariff      {shown}")
    lines.append(f"sha256      {index.sha256}")
    lines.append(f"nodes       {len(index.nodes):,}   terminals {index.terminal_count:,}")
    lines.append("")

    by_file = Counter(c.source_file for c in cases)
    lines.append(f"cases       {total} in {len(by_file)} file(s)")
    lines.append("")
    lines.extend(_histogram("by file", by_file))
    lines.append("")
    lines.extend(_histogram("by outcome", Counter(c.expect_outcome for c in cases), list(OUTCOMES)))
    lines.append("")
    lines.extend(_histogram("by source", Counter(c.source for c in cases), list(SOURCES)))
    lines.append("")

    tags: Counter[str] = Counter()
    for case in cases:
        tags.update(case.tags)
    lines.extend(_histogram("by tag", tags))
    lines.append("")

    # Section coverage: the check that the set is not all plastics.
    per_section: Counter[str] = Counter()
    no_code = 0
    for case in cases:
        code = case.expect_code or case.expect_heading
        section = index.section_of(code) if code else None
        if section is None:
            no_code += 1
        else:
            per_section[section] += 1

    lines.append(f"by tariff section  ({len(per_section)}/{len(index.sections)} covered)")
    for code in sorted(index.sections):
        count = per_section.get(code, 0)
        description = index.sections[code].description
        marker = " " if count else "!"
        lines.append(f" {marker}{code}  {count:>3}  {description[:56]}")
    lines.append(f"  --   {no_code:>3}  (no code expected: clarification / conversation)")
    lines.append("")

    granularity = Counter(
        "10-digit" if c.expect_code else "4-digit heading" if c.expect_heading else "outcome only"
        for c in cases
    )
    lines.extend(
        _histogram(
            "by expectation granularity",
            granularity,
            ["10-digit", "4-digit heading", "outcome only"],
        )
    )
    lines.append("")

    if report.warnings:
        lines.append(f"warnings ({len(report.warnings)})")
        lines.extend(f"  ! {w}" for w in report.warnings)
        lines.append("")

    if report.failures:
        lines.append(f"FAILURES ({len(report.failures)})")
        lines.extend(f"  x {f}" for f in report.failures)
        lines.append("")
        lines.append("FAIL — the golden set is not trustworthy until every failure is fixed.")
    else:
        checked = sum(len(c.all_codes) for c in cases)
        headings = sum(1 for c in cases if c.expect_heading)
        lines.append(
            f"PASS — {checked} code(s) exist and are terminal, "
            f"{headings} heading(s) exist, {total} id(s) unique."
        )
    return "\n".join(lines)


def duplicate_code_map(cases: list[GoldenCase]) -> dict[str, list[str]]:
    """Codes expected by more than one case. Informational; the harness may want it."""
    owners: dict[str, list[str]] = defaultdict(list)
    for case in cases:
        if case.expect_code:
            owners[case.expect_code].append(case.id)
    return {code: ids for code, ids in owners.items() if len(ids) > 1}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the UKTZED golden set against the tariff. Offline.",
    )
    parser.add_argument("--golden-dir", type=Path, default=DEFAULT_GOLDEN_DIR)
    parser.add_argument("--tariff", type=Path, default=DEFAULT_TARIFF)
    args = parser.parse_args(argv)

    try:
        cases = load_cases(args.golden_dir)
    except GoldenSetError as exc:
        print(exc, file=sys.stderr)
        return 1

    index = TariffIndex.load(args.tariff)
    report = verify(cases, index)
    print(format_report(report, args.tariff))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
