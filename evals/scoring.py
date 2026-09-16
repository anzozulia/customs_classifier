"""The metrics. This file defines what "better" means, so read it before you trust a number.

v1 had no evaluation at all: `test_agents.py` called eight functions that were never defined,
so it crashed on import in every commit, and 1,403 production classifications produced zero
measured accuracy. That is precisely why exactly one prompt line changed in ten commits — with
nothing to measure, every change is a coin flip nobody can call. This module is the answer, and
it is deliberately small: pure functions over a golden case and one observation, no I/O, no
model, no judge.

WHAT IS MEASURED, AND WHY EACH DENOMINATOR IS WHAT IT IS
--------------------------------------------------------

A turn ends in exactly one of three ways, decided by which terminal tool the agent called:
`emit_classification` -> "result", `ask_clarification` -> "clarification", neither ->
"conversation" (plus "error", the failure branch). **A clarification is not a failure.** In v1
production 17.4% of turns were clarifications and the loop is part of the product. An eval that
scores a clarification as a wrong answer optimises the system into confident guessing, and a
wrong customs code has legal and financial consequences — it is the single worst outcome here.

So the scorecard splits the two questions the metrics could otherwise blur:

* **Was the answer right?** — `exact_code`, `heading_accuracy`, `chapter_accuracy`, scored
  ONLY over cases that expect a code *and* on which the system actually emitted one. This is
  conditional accuracy: "when it answers, how often is it right". A clarification neither
  helps nor hurts it.
* **Did it answer when it should have?** — `answer_rate` (of the cases the golden set says are
  classifiable, how many got a code) and `outcome_match`. `answer_rate` is what stops the
  degenerate strategy the conditional denominator would otherwise reward: a system that asks
  for clarification on everything scores 100% accuracy on the two cases it answers and a
  catastrophic `answer_rate`, and the gate fails it.
* **Did it answer when it should NOT have?** — `over_confidence`, reported separately and
  prominently, because it is the metric that catches a system tuned into confident wrong
  answers.

`heading_accuracy` leads the report. In customs work a heading-level hit is a near miss a human
finishes in seconds; a wrong chapter is a different product. `exact_code` is the stricter number
underneath it and `chapter_accuracy` the coarse sanity floor.

`groundedness` is a lookup, never an LLM judge: does every emitted code exist in the tariff and
is it terminal? The terminal-tool gate (E1-E4 in `app/agent/tools_terminal.py`) already enforces
this, so it should read 100% by construction. If it ever does not, the gate has a hole, and the
report says so in capitals.

THE REGRESSION RULE
-------------------

One case flipping on a 40-case set is noise, not a regression: at n=40 and p=0.85 the binomial
standard error is 5.6 points, about 2.3 cases. So a higher-is-better metric regresses only when
it drops by MORE than one case's worth of rate, computed on the smaller of the two denominators
(the permissive side, which is the right side to be on for a false alarm that costs a human an
afternoon). `over_confidence` is lower-is-better and uses the same one-case tolerance in the
other direction. `groundedness` has no tolerance at all: anything below 1.0 fails, with or
without a baseline, because the gate is supposed to make it impossible.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final, Literal

from app.tariff.repo import normalize_code

__all__ = [
    "BASELINE_SCHEMA_VERSION",
    "EMPTY_BASELINE",
    "GATED_METRICS",
    "GROUNDEDNESS_FLOOR",
    "TOLERANCE_CASES",
    "Aggregates",
    "Case",
    "CaseError",
    "CaseResult",
    "Comparison",
    "CostStat",
    "Delta",
    "EmittedCode",
    "Metric",
    "Observation",
    "Report",
    "ReportMeta",
    "Stat",
    "ToolCall",
    "baseline_from_report",
    "build_report",
    "case_from_golden",
    "chapter_hit",
    "compare_to_baseline",
    "exact_code_hit",
    "filter_cases",
    "grounded",
    "heading_hit",
    "load_baseline",
    "load_cases",
    "outcome_match",
    "over_confident",
    "parse_case",
    "score_case",
]

# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------

ExpectedOutcome = Literal["result", "clarification", "conversation"]
"""What a golden case says the turn SHOULD do. "error" is never expected — it is an outcome
the system produces, never one the golden set asks for."""

EXPECTED_OUTCOMES: Final[tuple[str, ...]] = ("result", "clarification", "conversation")
OBSERVED_OUTCOMES: Final[tuple[str, ...]] = ("result", "clarification", "conversation", "error")

BASELINE_SCHEMA_VERSION: Final[int] = 1

TOLERANCE_CASES: Final[float] = 1.0
"""How many cases' worth of rate a metric may lose before it counts as a regression."""

GROUNDEDNESS_FLOOR: Final[float] = 1.0
"""Groundedness is enforced by the terminal-tool gate. Below this is a P0, always."""

GATED_METRICS: Final[tuple[str, ...]] = (
    "exact_code",
    "heading_accuracy",
    "chapter_accuracy",
    "answer_rate",
    "outcome_match",
)
"""Higher-is-better metrics the regression gate reads. `over_confidence` (lower is better) and
`groundedness` (hard floor) are gated too, by their own rules in `compare_to_baseline`."""

_EPSILON: Final[float] = 1e-9
_MICRO_USD: Final = Decimal("0.000001")


class CaseError(ValueError):
    """A golden case is malformed. Loud and early: a case that scores nothing is worse than
    no case at all, because it silently shrinks the denominator."""


# --------------------------------------------------------------------------------------
# The golden case
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    """One golden case.

    Cases live in `evals/golden/` as `.yaml`, `.json` or `.jsonl`; a file is a list of these
    objects (a `.json` file may also wrap them in `{"cases": [...]}`).

    ```yaml
    - id: plastics-001
      input: "Пластикова кришка для харчового контейнера, поліпропілен"
      expect_outcome: result
      expect_code: "3923500000"
      accept_codes: ["3923900000"]
      tags: [plastics]
      source: domain-reasoning
      rationale: >-
        why this case exists and where the ground truth came from
    ```

    `id` is required and is the join key with the baseline, so renaming one is a new case.
    `input` may also be spelled `input_text`, and `rationale` may be spelled `note`.
    Everything else is optional:

    * `expect_outcome` defaults to "result" when a code or heading is expected and must be
      given explicitly otherwise. A case that expects "clarification" MUST NOT carry a code —
      "too vague to classify" and "the answer is 3923500000" cannot both be true, and a golden
      set that says both cannot be scored.
    * `accept_codes` are the other codes that count as exactly right. This is where sibling
      codes belong: 2,487 terminals (23.71%) share a `full_path` with a sibling because the
      snapshot dropped the header text that separates them, and the agent may legitimately
      emit any of them.
    * `expect_heading` / `expect_chapter` default to the first 4 / 2 digits of `expect_code`,
      because the first 4 digits of a 10-digit code ARE its heading and the first 2 ARE its
      chapter. Set `expect_heading` alone for a case where the heading is known but the last
      six digits depend on a detail the description does not carry.
    """

    id: str
    input: str
    expect_outcome: ExpectedOutcome = "result"
    expect_code: str | None = None
    accept_codes: tuple[str, ...] = ()
    expect_heading: str | None = None
    expect_chapter: str | None = None
    tags: tuple[str, ...] = ()
    note: str = ""
    source: str = ""
    """Where the ground truth came from — `verified-live`, `domain-reasoning`, … The golden
    set's own provenance field, carried through to the report so a disputed case can be
    traced back to whoever asserted it."""
    location: str = ""
    """`file.yaml[3]` — where this case is written down. For error messages only."""

    @property
    def expects_code(self) -> bool:
        """Does this case assert something about a code at all?"""
        return self.expect_outcome == "result" and bool(
            self.expect_code or self.expect_heading or self.expect_chapter
        )

    @property
    def accepted_codes(self) -> tuple[str, ...]:
        """Every 10-digit code that counts as exactly right, `expect_code` first."""
        codes = ([self.expect_code] if self.expect_code else []) + list(self.accept_codes)
        return tuple(dict.fromkeys(codes))

    @property
    def accepted_headings(self) -> tuple[str, ...]:
        """Every 4-digit heading that counts as a heading-level hit."""
        headings = [code[:4] for code in self.accepted_codes]
        if self.expect_heading:
            headings.append(self.expect_heading)
        return tuple(dict.fromkeys(headings))

    @property
    def accepted_chapters(self) -> tuple[str, ...]:
        """Every 2-digit chapter that counts as a chapter-level hit."""
        chapters = [heading[:2] for heading in self.accepted_headings]
        if self.expect_chapter:
            chapters.append(self.expect_chapter)
        return tuple(dict.fromkeys(chapters))


def parse_case(raw: Mapping[str, Any], *, location: str = "") -> Case:
    """One mapping -> one `Case`, with every contradiction rejected here rather than silently
    scored as a miss later. `location` is where the case is written down, for the messages."""
    source = location
    if not isinstance(raw, Mapping):  # a list item that is not an object
        raise CaseError(f"{source}: a case must be a mapping, got {type(raw).__name__}")

    case_id = str(raw.get("id") or "").strip()
    if not case_id:
        raise CaseError(f"{source}: 'id' is required (it is the join key with the baseline)")

    text = str(raw.get("input") or raw.get("input_text") or "").strip()
    if not text:
        raise CaseError(f"{case_id} ({source}): 'input' is required and must not be empty")

    expect_code = _digits_or_none(raw.get("expect_code"), field_name="expect_code", case=case_id)
    accept_codes = tuple(
        digits
        for value in _as_list(raw.get("accept_codes"))
        if (digits := _digits_or_none(value, field_name="accept_codes", case=case_id))
    )
    expect_heading = _digits_or_none(
        raw.get("expect_heading"), field_name="expect_heading", case=case_id
    )
    expect_chapter = _digits_or_none(
        raw.get("expect_chapter"), field_name="expect_chapter", case=case_id
    )

    for name, value, width in (
        ("expect_code", expect_code, 10),
        ("expect_heading", expect_heading, 4),
        ("expect_chapter", expect_chapter, 2),
        *(("accept_codes", code, 10) for code in accept_codes),
    ):
        if value and len(value) != width:
            raise CaseError(f"{case_id} ({source}): {name} must be {width} digits, got {value!r}")

    declared = raw.get("expect_outcome")
    has_expectation = bool(expect_code or expect_heading or expect_chapter)
    outcome = str(declared).strip() if declared else ("result" if has_expectation else "")
    if not outcome:
        raise CaseError(
            f"{case_id} ({source}): 'expect_outcome' is required when no code is expected — "
            f"one of {', '.join(EXPECTED_OUTCOMES)}"
        )
    if outcome not in EXPECTED_OUTCOMES:
        raise CaseError(
            f"{case_id} ({source}): expect_outcome {outcome!r} is not one of "
            f"{', '.join(EXPECTED_OUTCOMES)}"
        )
    if outcome != "result" and has_expectation:
        # The contradiction that would quietly poison over_confidence: a case cannot be both
        # too vague to classify and have a known answer.
        raise CaseError(
            f"{case_id} ({source}): expect_outcome={outcome!r} cannot carry an expected code — "
            "either the input is classifiable or it is not"
        )

    tags = tuple(str(tag).strip() for tag in _as_list(raw.get("tags") or raw.get("tag")) if tag)
    return Case(
        id=case_id,
        input=text,
        expect_outcome=outcome,  # type: ignore[arg-type]
        expect_code=expect_code,
        accept_codes=accept_codes,
        expect_heading=expect_heading or (expect_code[:4] if expect_code else None),
        expect_chapter=expect_chapter
        or (expect_heading[:2] if expect_heading else None)
        or (expect_code[:2] if expect_code else None),
        tags=tags,
        note=str(raw.get("note") or raw.get("rationale") or "").strip(),
        source=str(raw.get("source") or "").strip(),
        location=location,
    )


CASE_SUFFIXES: Final[frozenset[str]] = frozenset({".yaml", ".yml", ".json", ".jsonl"})
"""What a golden file may be written in. The set ships as YAML, because a case carries a
paragraph of Ukrainian tariff reasoning and JSON has no readable way to hold one."""


def case_from_golden(golden: Any, *, location: str = "") -> Case:
    """`evals.build_golden.GoldenCase` -> `Case`.

    Duck-typed on purpose: the golden set's loader owns its own dataclass, and reading it
    through `getattr` means a field added there cannot break a run here. Everything still goes
    through `parse_case`, so the scorer's own rules — a code is ten digits, a clarification
    case may not carry one — are applied to both loaders' output identically.
    """
    return parse_case(
        {
            "id": getattr(golden, "id", ""),
            "input": getattr(golden, "input", ""),
            "expect_outcome": getattr(golden, "expect_outcome", ""),
            "expect_code": getattr(golden, "expect_code", None),
            "expect_heading": getattr(golden, "expect_heading", None),
            "accept_codes": list(getattr(golden, "accept_codes", ()) or ()),
            "tags": list(getattr(golden, "tags", ()) or ()),
            "rationale": getattr(golden, "rationale", ""),
            "source": getattr(golden, "source", ""),
        },
        location=location or str(getattr(golden, "source_file", "") or ""),
    )


def _load_via_golden_builder(root: Path) -> list[Case] | None:
    """Load a YAML golden directory through `evals.build_golden`, its own schema authority.

    `evals/README.md` states it plainly: `build_golden.py` is the single implementation of the
    golden-set schema and the harness should not re-parse the YAML. That loader is strict where
    this module is tolerant — it rejects unknown fields, demands a rationale and a source, and
    reports every structural problem at once instead of the first — and running it on every
    eval run is how a typo in a case file fails loudly instead of silently changing a
    denominator.

    `None` means "not applicable, use the plain reader": no YAML here (a `.json` fixture
    directory), or `build_golden` is not importable. A YAML directory that IS there and does
    not load is a hard error, re-raised as `CaseError` so callers catch one type.
    """
    if not any(root.glob("*.yaml")):
        return None
    try:
        from evals import build_golden
    except ModuleNotFoundError:  # pragma: no cover - the builder ships alongside this file
        return None
    try:
        golden_cases = build_golden.load_cases(root)
    except build_golden.GoldenSetError as exc:
        raise CaseError(str(exc)) from exc
    return [case_from_golden(case) for case in golden_cases]


def load_cases(path: str | Path) -> list[Case]:
    """Load every case under a directory, or from one file. Order is stable: files sorted by
    name, cases in file order, so `--limit` means the same thing on every run.

    A directory of `.yaml` files is handed to `evals.build_golden` (see
    `_load_via_golden_builder`); anything else — a single file, `.json`, `.jsonl` — is read
    here. Both paths end in `parse_case`, so there is one set of scoring rules either way.
    """
    root = Path(path)
    if root.is_dir():
        delegated = _load_via_golden_builder(root)
        if delegated is not None:
            _reject_duplicate_ids(delegated)
            return delegated
        files = sorted(
            child for child in root.iterdir() if child.is_file() and child.suffix in CASE_SUFFIXES
        )
    elif root.is_file():
        files = [root]
    else:
        raise CaseError(f"no such case file or directory: {root}")

    cases = [
        parse_case(raw, location=f"{file.name}[{index}]")
        for file in files
        for index, raw in enumerate(_read_case_file(file))
    ]
    _reject_duplicate_ids(cases)
    return cases


def _reject_duplicate_ids(cases: Sequence[Case]) -> None:
    """Ids are the baseline's join key. Two cases sharing one would make every comparison
    against the baseline quietly meaningless, in a way no metric would show."""
    seen: dict[str, str] = {}
    for case in cases:
        if case.id in seen:
            raise CaseError(
                f"duplicate case id {case.id!r} in {case.location or '?'} — already defined "
                f"in {seen[case.id] or '?'}; ids are the baseline's join key"
            )
        seen[case.id] = case.location


def filter_cases(
    cases: Sequence[Case], *, tag: str | None = None, limit: int | None = None
) -> list[Case]:
    """`--tag` then `--limit`, in that order. `--limit` keeps the first N in load order."""
    rows = [case for case in cases if tag is None or tag in case.tags]
    return rows[:limit] if limit is not None and limit >= 0 else rows


def _read_case_file(file: Path) -> list[Any]:
    text = file.read_text(encoding="utf-8")
    if file.suffix in {".yaml", ".yml"}:
        # Imported here, not at the top: `evals.scoring` must stay importable with nothing but
        # the runtime dependencies installed, and PyYAML is a tooling dependency of the golden
        # set. `safe_load` never constructs Python objects out of the file.
        try:
            import yaml
        except ModuleNotFoundError as exc:  # pragma: no cover - environment, not logic
            raise CaseError(
                f"{file.name} is YAML but PyYAML is not installed — `pip install pyyaml`"
            ) from exc
        try:
            payload = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise CaseError(f"{file.name}: {exc}") from exc
        if payload is None:
            return []
        if isinstance(payload, Mapping):
            return list(payload["cases"]) if "cases" in payload else [payload]
        if isinstance(payload, list):
            return list(payload)
        raise CaseError(f"{file.name}: expected a list of cases")
    if file.suffix == ".jsonl":
        rows: list[Any] = []
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise CaseError(f"{file.name}:{number}: {exc}") from exc
        return rows
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CaseError(f"{file.name}: {exc}") from exc
    if isinstance(payload, Mapping):
        # `{"cases": [...]}` so a file can carry a header comment, or a single bare case.
        return list(payload["cases"]) if "cases" in payload else [payload]
    if isinstance(payload, list):
        return list(payload)
    raise CaseError(f"{file.name}: expected an object or a list of cases")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return list(value)
    return [value]


def _digits_or_none(value: Any, *, field_name: str, case: str) -> str | None:
    """Accept `"8517 13 00 00"` as well as `8517130000`, reject anything that is not digits."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise CaseError(f"{case}: {field_name} must be a string of digits, got {value!r}")
    digits = normalize_code(str(value))
    if not digits:
        raise CaseError(f"{case}: {field_name} {value!r} contains no digits")
    return digits


# --------------------------------------------------------------------------------------
# The observation — what one run of one case produced
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call off the turn ledger: what was called, with what, for how long."""

    name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    duration_ms: int | None = None
    error: str | None = None

    @property
    def label(self) -> str:
        """`open_category(8517)` — the navigation trail, readable in a failure report."""
        for key in ("category_code", "prefix", "code", "group_code", "section_code", "query"):
            value = self.args.get(key)
            if value:
                return f"{self.name}({value})"
        return self.name


@dataclass(frozen=True, slots=True)
class EmittedCode:
    """One emitted code, already looked up in the tariff by the harness.

    `status` is `app.tariff.validate.CodeStatus`'s value — the SAME function the terminal tool
    gate uses, so groundedness is the gate's own question asked a second time, in SQL.
    """

    code: str
    status: str = "valid"
    is_terminal: bool = True
    full_path: str = ""

    @property
    def is_grounded(self) -> bool:
        """Exists in the dataset and is terminal. `ambiguous` passes: the code is real and
        final, it is the snapshot that cannot separate it from its siblings by text."""
        return self.status in {"valid", "ambiguous"} and self.is_terminal


@dataclass(frozen=True, slots=True)
class Observation:
    """What one case produced, read off the product's own instrumentation.

    Built by `evals/harness.py` from the turn ledger and the `uktzed.chat` turn summary — the
    same two sources `app/records/writer.py` persists from — so an eval row and a production
    row are the same numbers read the same way.
    """

    case_id: str
    outcome: str = "conversation"
    codes: tuple[EmittedCode, ...] = ()
    turns: int = 0
    repairs: int = 0
    tool_calls: tuple[ToolCall, ...] = ()
    tokens_in: int = 0
    tokens_cached: int = 0
    tokens_out: int = 0
    cost_usd: Decimal | None = None
    latency_ms: int = 0
    ttfb_ms: int | None = None
    wall_ms: int = 0
    error_class: str | None = None
    model: str = ""
    prompt_version: str = ""
    prompt_sha256: str = ""
    codes_seen: int = 0

    @property
    def code_list(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.codes)

    @property
    def primary_code(self) -> str | None:
        """The code the answer leads with — `position = 0` on the stored record.

        `emit_classification` may return up to three when the dataset cannot separate
        siblings; the first is the one the user reads first, so it is the one scored.
        """
        return self.codes[0].code if self.codes else None

    @property
    def answered(self) -> bool:
        return bool(self.codes) and self.outcome == "result"

    @property
    def path(self) -> str:
        """The navigation trail, for a failure you can actually diagnose."""
        return " -> ".join(call.label for call in self.tool_calls) or "(no tool calls)"


# --------------------------------------------------------------------------------------
# The metrics themselves — one function each, all pure
# --------------------------------------------------------------------------------------


def exact_code_hit(case: Case, obs: Observation) -> bool | None:
    """Did the primary emitted code match `expect_code` (or one of `accept_codes`)?

    `None` — not applicable — when the case expects no code, or when the system emitted none.
    The second half is the deliberate one: a clarification is not a wrong answer, so it leaves
    this metric alone and is counted by `answer_rate` instead.
    """
    if not case.expects_code or not case.accepted_codes or not obs.answered:
        return None
    return obs.primary_code in case.accepted_codes


def heading_hit(case: Case, obs: Observation) -> bool | None:
    """Do the first 4 digits match — the fairer metric, and the one the report leads with.

    A heading-level hit is a near miss a customs officer finishes in seconds. Pure string
    prefix work: the first 4 digits of a 10-digit code ARE its heading, so there is no lookup.
    """
    if not case.expects_code or not case.accepted_headings or not obs.answered:
        return None
    primary = obs.primary_code or ""
    return primary[:4] in case.accepted_headings


def chapter_hit(case: Case, obs: Observation) -> bool | None:
    """Do the first 2 digits match? A wrong chapter is a different product entirely."""
    if not case.expects_code or not case.accepted_chapters or not obs.answered:
        return None
    primary = obs.primary_code or ""
    return primary[:2] in case.accepted_chapters


def outcome_match(case: Case, obs: Observation) -> bool:
    """Did it classify when it should classify, and ask when it should ask?

    Applies to every case, including the ones that expect no code. An "error" outcome never
    matches: the golden set never asks for a failure.
    """
    return obs.outcome == case.expect_outcome


def over_confident(case: Case, obs: Observation) -> bool:
    """THE ONE THAT MATTERS MOST: it emitted a code when the golden set says the input was too
    vague to classify.

    A wrong customs code has legal and financial consequences, and this is the metric that
    catches a system optimised into confident guessing. Scored on emitted codes rather than on
    the outcome label, because emitting a code IS the harm — whatever the turn was labelled.
    """
    return case.expect_outcome == "clarification" and bool(obs.codes)


def missed_classification(case: Case, obs: Observation) -> bool:
    """It asked for clarification on an input the golden set says is classifiable.

    NOT the mirror image of over-confidence in severity: this costs the user one more turn,
    the other costs them a wrong code at customs. Tracked because a system that drifts into
    asking about everything is still broken, and `answer_rate` is where that shows up.
    """
    return case.expect_outcome == "result" and obs.outcome == "clarification"


def grounded(obs: Observation) -> bool | None:
    """Does every emitted code exist in the tariff and is it terminal?

    A lookup against the active dataset (`validate_and_resolve`), performed by the harness —
    NOT an LLM judge. `None` when nothing was emitted. This should be 100% by construction,
    because `emit_classification` refuses anything else; a violation means the gate has a hole
    and the report shouts about it.
    """
    if not obs.codes:
        return None
    return all(item.is_grounded for item in obs.codes)


# --------------------------------------------------------------------------------------
# Per-case result
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One scored case: what was expected, what happened, and every metric's verdict."""

    case: Case
    observation: Observation
    exact: bool | None
    heading: bool | None
    chapter: bool | None
    outcome_ok: bool
    over_confidence: bool
    missed: bool
    answered: bool | None
    grounded_ok: bool | None

    @property
    def case_id(self) -> str:
        return self.case.id

    @property
    def severity(self) -> int | None:
        """Lowest number is worst. `None` means nothing went wrong on this case.

        The order is the order a human should read the failures in: a groundedness violation
        is a hole in the gate, over-confidence is the outcome with legal consequences, an
        error is a turn the user never got, and after that it is simply how far off the code
        landed.
        """
        if self.grounded_ok is False:
            return 0
        if self.over_confidence:
            return 1
        if self.observation.error_class is not None:
            return 2
        if self.chapter is False:
            return 3
        if self.heading is False:
            return 4
        if self.exact is False:
            return 5
        if not self.outcome_ok:
            return 6
        return None

    @property
    def is_failure(self) -> bool:
        return self.severity is not None

    def to_dict(self) -> dict[str, Any]:
        obs = self.observation
        return {
            "case_id": self.case.id,
            "tags": list(self.case.tags),
            "source": self.case.source,
            "input": self.case.input,
            "expect": {
                "outcome": self.case.expect_outcome,
                "code": self.case.expect_code,
                "accept_codes": list(self.case.accept_codes),
                "heading": self.case.expect_heading,
                "chapter": self.case.expect_chapter,
            },
            "got": {
                "outcome": obs.outcome,
                "codes": [
                    {
                        "code": item.code,
                        "status": item.status,
                        "is_terminal": item.is_terminal,
                        "full_path": item.full_path,
                    }
                    for item in obs.codes
                ],
                "primary_code": obs.primary_code,
                "error_class": obs.error_class,
                "path": obs.path,
            },
            "metrics": {
                "exact_code": self.exact,
                "heading_accuracy": self.heading,
                "chapter_accuracy": self.chapter,
                "outcome_match": self.outcome_ok,
                "over_confidence": self.over_confidence,
                "missed_classification": self.missed,
                "answered": self.answered,
                "groundedness": self.grounded_ok,
            },
            "efficiency": {
                "turns": obs.turns,
                "repairs": obs.repairs,
                "tool_calls": len(obs.tool_calls),
                "latency_ms": obs.latency_ms,
                "ttfb_ms": obs.ttfb_ms,
                "wall_ms": obs.wall_ms,
                "tokens_in": obs.tokens_in,
                "tokens_cached": obs.tokens_cached,
                "tokens_out": obs.tokens_out,
                "cost_usd": _money(obs.cost_usd),
            },
            "trace": [
                {
                    "name": call.name,
                    "args": dict(call.args),
                    "duration_ms": call.duration_ms,
                    "error": call.error,
                }
                for call in obs.tool_calls
            ],
        }


def score_case(case: Case, obs: Observation) -> CaseResult:
    """Every metric for one case. The only place the per-case verdicts are assembled."""
    return CaseResult(
        case=case,
        observation=obs,
        exact=exact_code_hit(case, obs),
        heading=heading_hit(case, obs),
        chapter=chapter_hit(case, obs),
        outcome_ok=outcome_match(case, obs),
        over_confidence=over_confident(case, obs),
        missed=missed_classification(case, obs),
        answered=obs.answered if case.expects_code else None,
        grounded_ok=grounded(obs),
    )


# --------------------------------------------------------------------------------------
# Aggregates
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Metric:
    """`hits` out of `total`, plus the ids that count against it.

    `flagged` is always "the cases a human should look at": the misses for a higher-is-better
    metric, the offenders for `over_confidence` and `groundedness`.
    """

    name: str
    hits: int
    total: int
    flagged: tuple[str, ...] = ()
    higher_is_better: bool = True

    @property
    def rate(self) -> float | None:
        """`None` when nothing was applicable — never 0.0, which would read as a failure."""
        return self.hits / self.total if self.total else None

    def to_dict(self) -> dict[str, Any]:
        return {"hits": self.hits, "total": self.total, "rate": self.rate}


@dataclass(frozen=True, slots=True)
class Stat:
    """mean / p50 / p90 over one efficiency series."""

    n: int
    mean: float | None
    p50: float | None
    p90: float | None

    def to_dict(self) -> dict[str, Any]:
        return {"n": self.n, "mean": self.mean, "p50": self.p50, "p90": self.p90}


@dataclass(frozen=True, slots=True)
class CostStat:
    """The same, in money — Decimal throughout, because money is never a float."""

    n: int
    total: Decimal | None
    mean: Decimal | None
    p50: Decimal | None
    p90: Decimal | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "total": _money(self.total),
            "mean": _money(self.mean),
            "p50": _money(self.p50),
            "p90": _money(self.p90),
        }


@dataclass(frozen=True, slots=True)
class Aggregates:
    """Every headline number for one run."""

    exact_code: Metric
    heading_accuracy: Metric
    chapter_accuracy: Metric
    answer_rate: Metric
    outcome_match: Metric
    over_confidence: Metric
    missed_classification: Metric
    groundedness: Metric
    error_rate: Metric
    outcomes: Mapping[str, int]
    errors: Mapping[str, int]
    turns: Stat
    tool_calls: Stat
    latency_ms: Stat
    cost_usd: CostStat

    @property
    def metrics(self) -> dict[str, Metric]:
        return {
            "exact_code": self.exact_code,
            "heading_accuracy": self.heading_accuracy,
            "chapter_accuracy": self.chapter_accuracy,
            "answer_rate": self.answer_rate,
            "outcome_match": self.outcome_match,
            "over_confidence": self.over_confidence,
            "missed_classification": self.missed_classification,
            "groundedness": self.groundedness,
            "error_rate": self.error_rate,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": {name: metric.to_dict() for name, metric in self.metrics.items()},
            "flagged": {
                name: list(metric.flagged)
                for name, metric in self.metrics.items()
                if metric.flagged
            },
            "outcomes": dict(self.outcomes),
            "errors": dict(self.errors),
            "efficiency": {
                "turns": self.turns.to_dict(),
                "tool_calls": self.tool_calls.to_dict(),
                "latency_ms": self.latency_ms.to_dict(),
                "cost_usd": self.cost_usd.to_dict(),
            },
        }


def aggregate(results: Sequence[CaseResult]) -> Aggregates:
    """Roll per-case verdicts up into the scorecard. Every denominator is explicit here."""
    outcomes = {name: 0 for name in OBSERVED_OUTCOMES}
    errors: dict[str, int] = {}
    for result in results:
        outcomes[result.observation.outcome] = outcomes.get(result.observation.outcome, 0) + 1
        if result.observation.error_class:
            errors[result.observation.error_class] = (
                errors.get(result.observation.error_class, 0) + 1
            )

    expects_code = [r for r in results if r.case.expects_code]
    vague = [r for r in results if r.case.expect_outcome == "clarification"]
    emitted = [r for r in results if r.observation.codes]

    return Aggregates(
        exact_code=_tri_metric("exact_code", results, lambda r: r.exact),
        heading_accuracy=_tri_metric("heading_accuracy", results, lambda r: r.heading),
        chapter_accuracy=_tri_metric("chapter_accuracy", results, lambda r: r.chapter),
        # Coverage. Denominator: the cases the golden set says ARE classifiable. This is the
        # metric that stops "clarify on everything" from scoring 100% accuracy.
        answer_rate=Metric(
            name="answer_rate",
            hits=sum(1 for r in expects_code if r.observation.answered),
            total=len(expects_code),
            flagged=tuple(r.case_id for r in expects_code if not r.observation.answered),
        ),
        outcome_match=Metric(
            name="outcome_match",
            hits=sum(1 for r in results if r.outcome_ok),
            total=len(results),
            flagged=tuple(r.case_id for r in results if not r.outcome_ok),
        ),
        # Lower is better, so `hits` counts the harm. Denominator: the cases the golden set
        # marks as too vague — the only cases on which over-confidence is even possible.
        over_confidence=Metric(
            name="over_confidence",
            hits=sum(1 for r in vague if r.over_confidence),
            total=len(vague),
            flagged=tuple(r.case_id for r in vague if r.over_confidence),
            higher_is_better=False,
        ),
        missed_classification=Metric(
            name="missed_classification",
            hits=sum(1 for r in expects_code if r.missed),
            total=len(expects_code),
            flagged=tuple(r.case_id for r in expects_code if r.missed),
            higher_is_better=False,
        ),
        groundedness=Metric(
            name="groundedness",
            hits=sum(1 for r in emitted if r.grounded_ok),
            total=len(emitted),
            flagged=tuple(r.case_id for r in emitted if not r.grounded_ok),
        ),
        error_rate=Metric(
            name="error_rate",
            hits=sum(1 for r in results if r.observation.error_class),
            total=len(results),
            flagged=tuple(r.case_id for r in results if r.observation.error_class),
            higher_is_better=False,
        ),
        outcomes=outcomes,
        errors=errors,
        turns=_stat([float(r.observation.turns) for r in results]),
        tool_calls=_stat([float(len(r.observation.tool_calls)) for r in results]),
        latency_ms=_stat([float(r.observation.latency_ms) for r in results]),
        cost_usd=_cost_stat(
            [r.observation.cost_usd for r in results if r.observation.cost_usd is not None]
        ),
    )


def _tri_metric(
    name: str, results: Sequence[CaseResult], pick: Callable[[CaseResult], bool | None]
) -> Metric:
    """A metric whose per-case verdict is True / False / not-applicable.

    Not-applicable cases leave the denominator alone. That is the whole point of the
    three-valued verdict: a clarification on a code-expecting case is scored by `answer_rate`,
    not smuggled into accuracy as a wrong code.
    """
    applicable = [(r, pick(r)) for r in results]
    scored = [(r, value) for r, value in applicable if value is not None]
    return Metric(
        name=name,
        hits=sum(1 for _, value in scored if value),
        total=len(scored),
        flagged=tuple(r.case_id for r, value in scored if not value),
    )


def _percentile[T: (float, Decimal)](values: Sequence[T], q: float) -> T | None:
    """Nearest-rank percentile: no interpolation, so every reported value is one a run
    actually produced. p50 of an even-sized set is the lower middle — "3.5 turns" is a number
    no turn ever took."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _stat(values: Sequence[float]) -> Stat:
    if not values:
        return Stat(n=0, mean=None, p50=None, p90=None)
    return Stat(
        n=len(values),
        mean=sum(values) / len(values),
        p50=_percentile(values, 0.5),
        p90=_percentile(values, 0.9),
    )


def _cost_stat(values: Sequence[Decimal]) -> CostStat:
    if not values:
        return CostStat(n=0, total=None, mean=None, p50=None, p90=None)
    total = sum(values, Decimal(0))
    # Quantized on the way out, never on the way in: every figure here is a micro-dollar
    # `NUMERIC(10,6)`, the same shape `classification.cost_usd` stores.
    return CostStat(
        n=len(values),
        total=_micro(total),
        mean=_micro(total / len(values)),
        p50=_micro(_percentile(values, 0.5)),
        p90=_micro(_percentile(values, 0.9)),
    )


def _micro(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_MICRO_USD, rounding=ROUND_HALF_UP)


def _money(value: Decimal | None) -> str | None:
    """Money crosses the JSON boundary as a string. A float would round it, and this number
    ends up in a budget."""
    return None if value is None else f"{value:f}"


# --------------------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReportMeta:
    """Which system produced these numbers. Without this a baseline is a number with no
    provenance — exactly v1's problem, where four documents gave four accounts of what ran."""

    generated_at: str = ""
    model: str | None = None
    prompt_version: str | None = None
    prompt_sha256: str | None = None
    dataset_sha256: str | None = None
    cases_path: str | None = None
    tag: str | None = None
    concurrency: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
            "model": self.model,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "dataset_sha256": self.dataset_sha256,
            "cases_path": self.cases_path,
            "tag": self.tag,
            "concurrency": self.concurrency,
        }


@dataclass(frozen=True, slots=True)
class Report:
    """Per-case rows plus aggregates, renderable as a text table AND as JSON."""

    meta: ReportMeta
    rows: tuple[CaseResult, ...]
    aggregates: Aggregates

    @property
    def failures(self) -> list[CaseResult]:
        """Worst first, then by case id, so two runs of the same set read the same way."""
        return sorted(
            (row for row in self.rows if row.is_failure),
            key=lambda row: (row.severity if row.severity is not None else 99, row.case_id),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "meta": self.meta.to_dict(),
            "case_count": len(self.rows),
            "case_ids": [row.case_id for row in self.rows],
            **self.aggregates.to_dict(),
            "cases": [row.to_dict() for row in self.rows],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent) + "\n"

    def render(self, *, max_failures: int = 20) -> str:
        return _render_report(self, max_failures=max_failures)


def build_report(
    pairs: Iterable[tuple[Case, Observation]], meta: ReportMeta | None = None
) -> Report:
    """Score every (case, observation) pair and roll it up."""
    rows = tuple(score_case(case, obs) for case, obs in pairs)
    resolved = meta or ReportMeta()
    if not resolved.generated_at:
        resolved = ReportMeta(
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            model=resolved.model,
            prompt_version=resolved.prompt_version,
            prompt_sha256=resolved.prompt_sha256,
            dataset_sha256=resolved.dataset_sha256,
            cases_path=resolved.cases_path,
            tag=resolved.tag,
            concurrency=resolved.concurrency,
        )
    return Report(meta=resolved, rows=rows, aggregates=aggregate(rows))


def _pct(rate: float | None) -> str:
    return "  n/a " if rate is None else f"{rate * 100:5.1f}%"


def _metric_line(label: str, metric: Metric) -> str:
    return f"  {label:<22} {metric.hits:>3}/{metric.total:<3} {_pct(metric.rate)}"


def _number(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _render_report(report: Report, *, max_failures: int = 20) -> str:
    agg = report.aggregates
    meta = report.meta
    sha = (meta.prompt_sha256 or "")[:12]
    dataset = (meta.dataset_sha256 or "")[:12]
    lines = [
        "=" * 96,
        f"UKTZED EVAL — {len(report.rows)} cases · model {meta.model or '?'} · "
        f"prompt {meta.prompt_version or '?'}/{sha or '?'} · dataset {dataset or '?'}",
        f"generated {meta.generated_at}"
        + (f" · tag {meta.tag}" if meta.tag else "")
        + (f" · concurrency {meta.concurrency}" if meta.concurrency else ""),
        "=" * 96,
        "ACCURACY (of the cases that expect a code and got one — a clarification is not a "
        "wrong answer)",
        _metric_line("heading_accuracy", agg.heading_accuracy) + "   <- lead metric",
        _metric_line("exact_code", agg.exact_code),
        _metric_line("chapter_accuracy", agg.chapter_accuracy),
        "",
        "BEHAVIOUR",
        _metric_line("answer_rate", agg.answer_rate) + "   (classifiable cases that got a code)",
        _metric_line("outcome_match", agg.outcome_match),
        _metric_line("missed_clarify", agg.missed_classification)
        + "   (asked about a classifiable input; lower is better)",
        _metric_line("error_rate", agg.error_rate) + "   (lower is better)",
        "",
        "OVER-CONFIDENCE — emitted a code when the golden set says the input was too vague",
        _metric_line("over_confidence", agg.over_confidence)
        + (
            "   *** " + ", ".join(agg.over_confidence.flagged)
            if agg.over_confidence.flagged
            else "   (none)"
        ),
        "",
        "GROUNDEDNESS — every emitted code exists in the tariff and is terminal (SQL, not a judge)",
    ]
    if agg.groundedness.total and agg.groundedness.hits < agg.groundedness.total:
        lines += [
            _metric_line("groundedness", agg.groundedness),
            "  *** P0: THE TERMINAL-TOOL GATE LET AN UNGROUNDED CODE THROUGH ***",
            "  " + ", ".join(agg.groundedness.flagged),
        ]
    else:
        lines.append(_metric_line("groundedness", agg.groundedness) + "   (as designed)")

    outcomes = " · ".join(f"{name} {count}" for name, count in agg.outcomes.items() if count)
    errors = " · ".join(f"{name} {count}" for name, count in agg.errors.items()) or "none"
    lines += [
        "",
        "EFFICIENCY",
        f"  turns       mean {_number(agg.turns.mean)}  p50 {_number(agg.turns.p50)}  "
        f"p90 {_number(agg.turns.p90)}",
        f"  tool calls  mean {_number(agg.tool_calls.mean)}  p50 {_number(agg.tool_calls.p50)}  "
        f"p90 {_number(agg.tool_calls.p90)}",
        f"  latency     mean {_seconds(agg.latency_ms.mean)}  p50 {_seconds(agg.latency_ms.p50)}  "
        f"p90 {_seconds(agg.latency_ms.p90)}",
        f"  cost        total ${_money(agg.cost_usd.total) or 'n/a'}  "
        f"mean ${_money(agg.cost_usd.mean) or 'n/a'}  p90 ${_money(agg.cost_usd.p90) or 'n/a'}",
        f"  outcomes    {outcomes or 'none'}",
        f"  errors      {errors}",
    ]

    failures = report.failures
    lines += ["", f"FAILURES ({len(failures)})" if failures else "FAILURES (none)"]
    for row in failures[:max_failures]:
        lines += _render_failure(row)
    if len(failures) > max_failures:
        lines.append(f"  … and {len(failures) - max_failures} more (see --json)")
    lines.append("=" * 96)
    return "\n".join(lines)


def _seconds(ms: float | None) -> str:
    return "n/a" if ms is None else f"{ms / 1000:.1f}s"


def _verdict(value: bool | None) -> str:
    return {True: "ok", False: "FAIL", None: "n/a"}[value]


def _render_failure(row: CaseResult) -> list[str]:
    case, obs = row.case, row.observation
    expected_code = case.expect_code or (f"{case.expect_heading}……" if case.expect_heading else "—")
    got_code = obs.primary_code or "—"
    tags = f" [{', '.join(case.tags)}]" if case.tags else ""
    lines = [
        f"  ({row.severity}) {case.id}{tags}",
        f"      expected  outcome={case.expect_outcome}  code={expected_code}"
        + (f"  accept={', '.join(case.accept_codes)}" if case.accept_codes else ""),
        f"      got       outcome={obs.outcome}  code={got_code}"
        + (f"  codes={', '.join(obs.code_list)}" if len(obs.codes) > 1 else "")
        + (f"  error={obs.error_class}" if obs.error_class else ""),
        f"      verdict   exact={_verdict(row.exact)} heading={_verdict(row.heading)} "
        f"chapter={_verdict(row.chapter)} outcome={_verdict(row.outcome_ok)} "
        f"grounded={_verdict(row.grounded_ok)}",
        f"      path      {obs.path}",
        f"      input     {case.input[:110]}",
    ]
    if obs.codes and obs.codes[0].full_path:
        lines.append(f"      got path  {obs.codes[0].full_path[:110]}")
    return lines


# --------------------------------------------------------------------------------------
# The baseline and the regression gate
# --------------------------------------------------------------------------------------

EMPTY_BASELINE: Final[dict[str, Any]] = {
    "schema_version": BASELINE_SCHEMA_VERSION,
    "comment": (
        "PLACEHOLDER — no scores yet. Populate from a real run: "
        "python -m evals.run --update-baseline. Numbers are null on purpose; an invented "
        "baseline is worse than none, because it makes the gate lie in both directions."
    ),
    "meta": ReportMeta().to_dict() | {"generated_at": None},
    "case_ids": [],
    "metrics": {},
    "efficiency": {},
    "tolerance": {
        "cases": TOLERANCE_CASES,
        "groundedness_floor": GROUNDEDNESS_FLOOR,
        "rule": (
            "A higher-is-better metric regresses only when it drops by MORE than one case's "
            "worth of rate, measured on the smaller of the two denominators; one case "
            "flipping on a 40-case set is noise (binomial SE at n=40, p=0.85 is ~2.3 cases). "
            "over_confidence is lower-is-better and uses the same tolerance in the other "
            "direction. groundedness has no tolerance: below 1.0 fails with or without a "
            "baseline, because the terminal-tool gate is supposed to make it impossible."
        ),
    },
}


def baseline_from_report(report: Report) -> dict[str, Any]:
    """The baseline document a passing run writes with `--update-baseline`."""
    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "comment": (
            "Recorded from a real run. Update it deliberately, in the same commit as the "
            "change that moved the numbers, and say why in the commit message."
        ),
        "meta": report.meta.to_dict(),
        "case_ids": [row.case_id for row in report.rows],
        "metrics": {name: metric.to_dict() for name, metric in report.aggregates.metrics.items()},
        "efficiency": report.aggregates.to_dict()["efficiency"],
        "tolerance": dict(EMPTY_BASELINE["tolerance"]),
    }


def load_baseline(path: str | Path) -> dict[str, Any]:
    """Read a baseline file. A missing file is not an error — it is the first run."""
    file = Path(path)
    if not file.exists():
        return {}
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CaseError(f"{file}: baseline is not valid JSON ({exc})") from exc
    return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True, slots=True)
class Delta:
    """One metric, before and after, and whether the difference is a regression."""

    metric: str
    baseline_rate: float | None
    current_rate: float | None
    tolerance: float
    regressed: bool
    reason: str = ""

    @property
    def change(self) -> float | None:
        if self.baseline_rate is None or self.current_rate is None:
            return None
        return self.current_rate - self.baseline_rate

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "baseline": self.baseline_rate,
            "current": self.current_rate,
            "change": self.change,
            "tolerance": self.tolerance,
            "regressed": self.regressed,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Comparison:
    """The gate's verdict. `ok is False` is what makes `evals/run.py` exit non-zero."""

    ok: bool
    deltas: tuple[Delta, ...]
    notes: tuple[str, ...] = ()

    @property
    def regressions(self) -> list[Delta]:
        return [delta for delta in self.deltas if delta.regressed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "regressions": [delta.to_dict() for delta in self.regressions],
            "deltas": [delta.to_dict() for delta in self.deltas],
            "notes": list(self.notes),
        }

    def render(self) -> str:
        lines = ["BASELINE", *[f"  note: {note}" for note in self.notes]]
        for delta in self.deltas:
            arrow = "  n/a" if delta.change is None else f"{delta.change * 100:+5.1f}pp"
            verdict = "REGRESSION" if delta.regressed else "ok"
            lines.append(
                f"  {delta.metric:<22} {_pct(delta.baseline_rate)} -> "
                f"{_pct(delta.current_rate)}  {arrow}  {verdict}"
                + (f"  — {delta.reason}" if delta.regressed and delta.reason else "")
            )
        lines.append("  VERDICT: " + ("PASS" if self.ok else "REGRESSION — exiting non-zero"))
        return "\n".join(lines)


def compare_to_baseline(report: Report, baseline: Mapping[str, Any]) -> Comparison:
    """Compare a run against a baseline and decide whether anything regressed.

    The rule, in full (and justified in this module's docstring):

    1. **Groundedness is absolute.** Below `GROUNDEDNESS_FLOOR` fails whether or not there is
       a baseline. The terminal-tool gate makes an ungrounded code impossible; one appearing
       means the gate has a hole, and no amount of statistical noise explains it.
    2. **One case is noise.** A higher-is-better metric regresses only when
       `(baseline - current) * min(denominators) > 1.0`. On a 40-case set that is a drop of
       more than 2.5 points; on a 10-case set, more than 10 points. Small sets are noisier and
       the rule is correspondingly more permissive, which is the right direction for a gate
       whose false alarm costs a human an afternoon.
    3. **Over-confidence is lower-is-better** and uses the same tolerance in the other
       direction: more than one extra vague input answered with a code is a regression.
    4. **An unpopulated baseline gates nothing** (rule 1 still applies). The first run records;
       it does not compare.
    5. **A changed case set is a note, not a failure.** The golden set is expected to grow, and
       a model swap (`--model`) is exactly the comparison this gate exists to make.
    """
    metrics = report.aggregates.metrics
    deltas: list[Delta] = []
    notes: list[str] = []

    baseline_metrics = baseline.get("metrics") or {}
    baseline_meta = baseline.get("meta") or {}

    if not baseline_metrics:
        notes.append(
            "baseline is not populated — nothing to compare against; run with "
            "--update-baseline once the numbers are real"
        )

    # Rule 5 — context, never a failure.
    baseline_ids = set(baseline.get("case_ids") or [])
    current_ids = {row.case_id for row in report.rows}
    if baseline_metrics and baseline_ids != current_ids:
        added = sorted(current_ids - baseline_ids)
        removed = sorted(baseline_ids - current_ids)
        notes.append(
            f"case set differs from the baseline (+{len(added)} / -{len(removed)}): "
            f"rates are comparable, per-case flips are not"
        )
    for field_name, label in (("model", "model"), ("prompt_version", "prompt")):
        was, now = baseline_meta.get(field_name), getattr(report.meta, field_name)
        if baseline_metrics and was and now and was != now:
            notes.append(f"{label} changed since the baseline: {was} -> {now}")

    # Rule 1 — absolute, and first, because it is the one that is never noise.
    groundedness = metrics["groundedness"]
    if groundedness.total and (groundedness.rate or 0.0) < GROUNDEDNESS_FLOOR - _EPSILON:
        deltas.append(
            Delta(
                metric="groundedness",
                baseline_rate=_baseline_rate(baseline_metrics, "groundedness"),
                current_rate=groundedness.rate,
                tolerance=0.0,
                regressed=True,
                reason=(
                    "P0: an emitted code is missing from the tariff or is not terminal — "
                    f"{', '.join(groundedness.flagged)}"
                ),
            )
        )
    else:
        deltas.append(
            Delta(
                metric="groundedness",
                baseline_rate=_baseline_rate(baseline_metrics, "groundedness"),
                current_rate=groundedness.rate,
                tolerance=0.0,
                regressed=False,
            )
        )

    # Rules 2 and 3.
    for name in (*GATED_METRICS, "over_confidence"):
        metric = metrics[name]
        base_rate = _baseline_rate(baseline_metrics, name)
        base_total = _baseline_total(baseline_metrics, name)
        tolerance = _tolerance_rate(base_total, metric.total)
        regressed = False
        reason = ""
        if base_rate is not None and metric.rate is not None:
            drop = base_rate - metric.rate if metric.higher_is_better else metric.rate - base_rate
            if drop > tolerance + _EPSILON:
                regressed = True
                cases = drop * max(1, min(base_total or metric.total, metric.total))
                reason = (
                    f"worse by {drop * 100:.1f}pp (~{cases:.1f} cases), tolerance is "
                    f"{tolerance * 100:.1f}pp (one case)"
                )
        deltas.append(
            Delta(
                metric=name,
                baseline_rate=base_rate,
                current_rate=metric.rate,
                tolerance=tolerance,
                regressed=regressed,
                reason=reason,
            )
        )

    return Comparison(
        ok=not any(delta.regressed for delta in deltas),
        deltas=tuple(deltas),
        notes=tuple(notes),
    )


def _baseline_rate(metrics: Mapping[str, Any], name: str) -> float | None:
    entry = metrics.get(name)
    if not isinstance(entry, Mapping):
        return None
    rate = entry.get("rate")
    return float(rate) if isinstance(rate, int | float) else None


def _baseline_total(metrics: Mapping[str, Any], name: str) -> int:
    entry = metrics.get(name)
    if not isinstance(entry, Mapping):
        return 0
    total = entry.get("total")
    return int(total) if isinstance(total, int) else 0


def _tolerance_rate(baseline_total: int, current_total: int) -> float:
    """One case's worth of rate, on the smaller of the two denominators.

    The smaller denominator gives the LARGER tolerance, which is the permissive side — a gate
    that cries wolf gets muted, and a muted gate measures nothing.
    """
    totals = [total for total in (baseline_total, current_total) if total > 0]
    return TOLERANCE_CASES / min(totals) if totals else 1.0
