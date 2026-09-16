"""The scorer, pinned. This is the test v1 never had, about the thing v1 never measured.

v1's `test_agents.py` called eight test functions that were never defined, so the suite crashed
on import in every commit and 1,403 classifications produced zero measured accuracy. The
counter-move is not "more tests" in general: it is that the file which decides what "better"
means is itself checked, offline, against fixtures — no API key, no database, no model.

Three properties matter more than the arithmetic and each has its own test below:

* **A clarification is never scored as a wrong answer.** 17.4% of v1's production turns were
  clarifications and the loop is part of the product. An eval that punishes them optimises the
  system into confident guessing, which in customs classification is the worst outcome there
  is. So accuracy is conditional on having answered, and `answer_rate` is what stops the
  degenerate "clarify on everything" strategy from scoring 100%.
* **Over-confidence is measured on emitting a code**, not on the outcome label, because
  emitting the code is the harm.
* **One case flipping is noise.** The regression gate has a stated tolerance and it is tested
  from both sides — a single flip passes, two fail — while groundedness has no tolerance at all.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from evals.scoring import (
    Case,
    CaseError,
    EmittedCode,
    Observation,
    ReportMeta,
    baseline_from_report,
    build_report,
    chapter_hit,
    compare_to_baseline,
    exact_code_hit,
    filter_cases,
    grounded,
    heading_hit,
    load_baseline,
    load_cases,
    missed_classification,
    outcome_match,
    over_confident,
    parse_case,
    score_case,
)

SMARTPHONE = "Смартфон Apple iPhone 15, корпус з алюмінію та скла, для стільникових мереж"
VAGUE = "Пластикова деталь"

PHONE_CODE = "8517130000"
PHONE_SIBLING = "8517140000"
LAPTOP_CODE = "8471300000"
PLASTIC_CODE = "3923500000"


def _case(case_id: str = "c1", **overrides: object) -> Case:
    payload: dict[str, object] = {"id": case_id, "input": SMARTPHONE, "expect_code": PHONE_CODE}
    payload.update(overrides)
    return parse_case(payload, location="fixture")


def _obs(
    case_id: str = "c1",
    *,
    outcome: str = "result",
    codes: tuple[str, ...] = (PHONE_CODE,),
    status: str = "valid",
    is_terminal: bool = True,
    turns: int = 3,
    tool_calls: tuple[str, ...] = ("open_category", "emit_classification"),
    cost: str | None = "0.010000",
    latency_ms: int = 5_000,
    error_class: str | None = None,
) -> Observation:
    from evals.scoring import ToolCall

    return Observation(
        case_id=case_id,
        outcome=outcome,
        codes=tuple(
            EmittedCode(code=code, status=status, is_terminal=is_terminal, full_path="a > b")
            for code in codes
        ),
        turns=turns,
        tool_calls=tuple(ToolCall(name=name, args={}) for name in tool_calls),
        cost_usd=Decimal(cost) if cost is not None else None,
        latency_ms=latency_ms,
        error_class=error_class,
    )


# ---------------------------------------------------------------------------------------
# Code matching: exact, heading, chapter
# ---------------------------------------------------------------------------------------


def test_exact_code_matches_the_primary_code() -> None:
    case = _case()
    assert exact_code_hit(case, _obs()) is True
    assert exact_code_hit(case, _obs(codes=(PHONE_SIBLING,))) is False


def test_accept_codes_count_as_exactly_right() -> None:
    """2,487 terminals share a `full_path` with a sibling; the golden set says which of them
    are equally correct, and any of those is a hit."""
    case = _case(accept_codes=[PHONE_SIBLING])
    assert exact_code_hit(case, _obs(codes=(PHONE_SIBLING,))) is True
    assert exact_code_hit(case, _obs(codes=(LAPTOP_CODE,))) is False


def test_only_the_primary_code_is_scored() -> None:
    """`emit_classification` may return up to three when the dataset cannot separate them.
    The first is the one the user reads first, so it is the one that counts — a correct code
    in second place is not a correct answer."""
    case = _case()
    assert exact_code_hit(case, _obs(codes=(PHONE_SIBLING, PHONE_CODE))) is False
    assert exact_code_hit(case, _obs(codes=(PHONE_CODE, PHONE_SIBLING))) is True


def test_heading_is_a_prefix_match_and_survives_a_wrong_tail() -> None:
    """The fairer metric, and the one the report leads with: a heading-level hit is a near
    miss a human finishes in seconds."""
    case = _case()
    near_miss = _obs(codes=(PHONE_SIBLING,))
    assert exact_code_hit(case, near_miss) is False
    assert heading_hit(case, near_miss) is True
    assert chapter_hit(case, near_miss) is True


def test_wrong_chapter_fails_every_level() -> None:
    case = _case()
    wrong = _obs(codes=(LAPTOP_CODE,))
    assert exact_code_hit(case, wrong) is False
    assert heading_hit(case, wrong) is False
    assert chapter_hit(case, wrong) is False


def test_expect_heading_alone_scores_the_heading_and_skips_exact() -> None:
    """A case whose last six digits depend on a detail the description does not carry: the
    heading is ground truth, the full code is not, and scoring it as exact would be a lie."""
    case = _case(expect_code=None, expect_heading="8517")
    observation = _obs(codes=(PHONE_SIBLING,))
    assert exact_code_hit(case, observation) is None
    assert heading_hit(case, observation) is True
    assert chapter_hit(case, observation) is True


def test_accept_codes_widen_the_heading_and_chapter_sets() -> None:
    case = _case(accept_codes=[PLASTIC_CODE])
    observation = _obs(codes=("3923900000",))
    assert exact_code_hit(case, observation) is False
    assert heading_hit(case, observation) is True
    assert chapter_hit(case, observation) is True


def test_codes_are_compared_as_digits_not_as_typed() -> None:
    """`8517 13 00 00` is the format a human writes; `normalize_code` is applied at the
    boundary so the golden set may use either."""
    case = _case(expect_code="8517 13 00 00")
    assert case.expect_code == PHONE_CODE
    assert exact_code_hit(case, _obs()) is True


# ---------------------------------------------------------------------------------------
# A clarification is not a wrong answer
# ---------------------------------------------------------------------------------------


def test_a_clarification_does_not_count_as_a_wrong_code() -> None:
    """THE property this whole design turns on. The case expects a code and the system asked
    instead: accuracy must be *not applicable*, not False. Scoring it False is what pushes a
    classifier into confident guessing."""
    case = _case()
    asked = _obs(outcome="clarification", codes=())
    assert exact_code_hit(case, asked) is None
    assert heading_hit(case, asked) is None
    assert chapter_hit(case, asked) is None
    assert missed_classification(case, asked) is True
    assert outcome_match(case, asked) is False


def test_an_errored_turn_is_not_scored_for_accuracy_either() -> None:
    case = _case()
    failed = _obs(outcome="error", codes=(), error_class="upstream_5xx")
    assert exact_code_hit(case, failed) is None
    assert outcome_match(case, failed) is False


def test_outcome_match_covers_the_non_classifying_branches() -> None:
    vague = parse_case({"id": "v", "input": VAGUE, "expect_outcome": "clarification"})
    chat = parse_case({"id": "q", "input": "Що таке УКТЗЕД?", "expect_outcome": "conversation"})
    assert outcome_match(vague, _obs(outcome="clarification", codes=())) is True
    assert outcome_match(vague, _obs(outcome="conversation", codes=())) is False
    assert outcome_match(chat, _obs(outcome="conversation", codes=())) is True


# ---------------------------------------------------------------------------------------
# Over-confidence — the metric that matters most
# ---------------------------------------------------------------------------------------


def test_over_confidence_fires_when_a_vague_input_gets_a_code() -> None:
    vague = parse_case({"id": "v", "input": VAGUE, "expect_outcome": "clarification"})
    assert over_confident(vague, _obs(codes=(PLASTIC_CODE,))) is True
    assert over_confident(vague, _obs(outcome="clarification", codes=())) is False


def test_over_confidence_is_measured_on_the_code_not_on_the_label() -> None:
    """Emitting the code IS the harm, whatever the turn ended up labelled. A run that emitted
    a code and then failed still put that code in front of the user."""
    vague = parse_case({"id": "v", "input": VAGUE, "expect_outcome": "clarification"})
    assert over_confident(vague, _obs(outcome="error", codes=(PLASTIC_CODE,))) is True


def test_a_classifiable_case_can_never_be_over_confident() -> None:
    assert over_confident(_case(), _obs()) is False


# ---------------------------------------------------------------------------------------
# Groundedness — a lookup, not a judge
# ---------------------------------------------------------------------------------------


def test_groundedness_passes_for_a_real_terminal_code() -> None:
    assert grounded(_obs()) is True


def test_groundedness_fails_for_a_code_that_is_not_in_the_dataset() -> None:
    """Should be unreachable: `emit_classification` gate E2 refuses it. If this ever fires in
    a real run the gate has a hole, and the report says so in capitals."""
    assert grounded(_obs(status="not_found", is_terminal=False)) is False


def test_groundedness_fails_for_an_intermediate_heading() -> None:
    assert grounded(_obs(status="not_terminal", is_terminal=False)) is False


def test_an_ambiguous_code_is_still_grounded() -> None:
    """`CodeStatus.AMBIGUOUS` is a PASS in `app/tariff/validate.py`: the code is real and
    final, it is the snapshot that cannot separate it from its siblings by text."""
    assert grounded(_obs(status="ambiguous")) is True


def test_a_failed_lookup_counts_as_ungrounded() -> None:
    assert grounded(_obs(status="lookup_failed", is_terminal=False)) is False


def test_groundedness_is_not_applicable_without_codes() -> None:
    assert grounded(_obs(outcome="clarification", codes=())) is None


# ---------------------------------------------------------------------------------------
# Aggregate maths
# ---------------------------------------------------------------------------------------


def _mixed_report() -> object:
    """Six cases: 2 right, 1 near miss, 1 wrong chapter, 1 classifiable-but-asked,
    1 vague-and-asked. Every denominator in the scorecard is exercised by this shape."""
    right_one = _case("right-1")
    right_two = _case("right-2")
    near = _case("near")
    wrong = _case("wrong")
    asked = _case("asked")
    vague = parse_case({"id": "vague", "input": VAGUE, "expect_outcome": "clarification"})
    return build_report(
        [
            (right_one, _obs("right-1", turns=2, latency_ms=1_000)),
            (right_two, _obs("right-2", turns=3, latency_ms=2_000)),
            (near, _obs("near", codes=(PHONE_SIBLING,), turns=4, latency_ms=3_000)),
            (wrong, _obs("wrong", codes=(LAPTOP_CODE,), turns=5, latency_ms=4_000)),
            (asked, _obs("asked", outcome="clarification", codes=(), turns=6, latency_ms=5_000)),
            (
                vague,
                _obs("vague", outcome="clarification", codes=(), turns=7, latency_ms=6_000),
            ),
        ],
        ReportMeta(generated_at="2026-09-16T00:00:00+00:00", model="gpt-5.6-terra"),
    )


def test_accuracy_denominators_exclude_the_cases_that_were_never_answered() -> None:
    agg = _mixed_report().aggregates  # type: ignore[attr-defined]
    # Four cases expected a code and got one; the fifth asked, the sixth was vague.
    assert (agg.exact_code.hits, agg.exact_code.total) == (2, 4)
    assert (agg.heading_accuracy.hits, agg.heading_accuracy.total) == (3, 4)
    assert (agg.chapter_accuracy.hits, agg.chapter_accuracy.total) == (3, 4)


def test_answer_rate_is_what_catches_clarify_on_everything() -> None:
    agg = _mixed_report().aggregates  # type: ignore[attr-defined]
    # Five cases are classifiable; four of them produced a code.
    assert (agg.answer_rate.hits, agg.answer_rate.total) == (4, 5)
    assert agg.answer_rate.flagged == ("asked",)
    assert (agg.missed_classification.hits, agg.missed_classification.total) == (1, 5)


def test_over_confidence_denominator_is_the_vague_cases_only() -> None:
    agg = _mixed_report().aggregates  # type: ignore[attr-defined]
    assert (agg.over_confidence.hits, agg.over_confidence.total) == (0, 1)
    assert agg.over_confidence.rate == 0.0


def test_outcome_and_groundedness_totals() -> None:
    agg = _mixed_report().aggregates  # type: ignore[attr-defined]
    assert (agg.outcome_match.hits, agg.outcome_match.total) == (5, 6)
    assert agg.outcome_match.flagged == ("asked",)
    # Four cases emitted codes; all four are grounded.
    assert (agg.groundedness.hits, agg.groundedness.total) == (4, 4)
    assert agg.outcomes["result"] == 4
    assert agg.outcomes["clarification"] == 2
    assert agg.error_rate.hits == 0


def test_flagged_ids_name_the_cases_a_human_should_open() -> None:
    agg = _mixed_report().aggregates  # type: ignore[attr-defined]
    assert agg.exact_code.flagged == ("near", "wrong")
    assert agg.heading_accuracy.flagged == ("wrong",)


def test_efficiency_percentiles_are_nearest_rank() -> None:
    """No interpolation: every number reported is one a run actually produced. Turns are
    2,3,4,5,6,7 -> p50 is the lower middle (4), p90 is the sixth of six (7)."""
    agg = _mixed_report().aggregates  # type: ignore[attr-defined]
    assert agg.turns.n == 6
    assert agg.turns.mean == pytest.approx(4.5)
    assert agg.turns.p50 == 4
    assert agg.turns.p90 == 7
    assert agg.latency_ms.p50 == 3_000  # 1..6s, lower middle again
    assert agg.latency_ms.p90 == 6_000
    assert agg.tool_calls.mean == pytest.approx(2.0)


def test_cost_is_decimal_all_the_way_through() -> None:
    """Money is never a float — it crosses the JSON boundary as a string."""
    report = _mixed_report()
    agg = report.aggregates  # type: ignore[attr-defined]
    assert agg.cost_usd.total == Decimal("0.060000")
    assert agg.cost_usd.mean == Decimal("0.010000")
    payload = json.loads(report.to_json())  # type: ignore[attr-defined]
    assert payload["efficiency"]["cost_usd"]["total"] == "0.060000"


def test_a_metric_with_no_applicable_cases_has_no_rate() -> None:
    """`None`, never 0.0 — a rate of zero reads as total failure."""
    chat = parse_case({"id": "q", "input": "Що таке УКТЗЕД?", "expect_outcome": "conversation"})
    agg = build_report([(chat, _obs("q", outcome="conversation", codes=()))]).aggregates
    assert agg.exact_code.total == 0
    assert agg.exact_code.rate is None
    assert agg.groundedness.rate is None


def test_failures_are_ordered_worst_first() -> None:
    """Groundedness (a hole in the gate) before over-confidence (a wrong code at customs)
    before a wrong chapter before a wrong code."""
    vague = parse_case({"id": "vague", "input": VAGUE, "expect_outcome": "clarification"})
    report = build_report(
        [
            (_case("near"), _obs("near", codes=(PHONE_SIBLING,))),
            (_case("chapter"), _obs("chapter", codes=(LAPTOP_CODE,))),
            (vague, _obs("vague", codes=(PLASTIC_CODE,))),
            (_case("ghost"), _obs("ghost", status="not_found", is_terminal=False)),
        ]
    )
    assert [row.case_id for row in report.failures] == ["ghost", "vague", "chapter", "near"]
    assert [row.severity for row in report.failures] == [0, 1, 3, 5]


def test_a_clean_case_is_not_a_failure() -> None:
    report = build_report([(_case(), _obs())])
    assert report.failures == []
    assert report.rows[0].is_failure is False


def test_the_text_report_renders_the_headline_and_the_failures() -> None:
    """A failure you cannot diagnose is not useful: the rendered report has to carry the
    expected/got pair and the navigation path."""
    text = _mixed_report().render()  # type: ignore[attr-defined]
    assert "heading_accuracy" in text
    assert "OVER-CONFIDENCE" in text
    assert "GROUNDEDNESS" in text
    assert "wrong" in text
    assert "open_category" in text


def test_the_report_survives_json_round_tripping() -> None:
    payload = json.loads(_mixed_report().to_json())  # type: ignore[attr-defined]
    assert payload["case_count"] == 6
    assert payload["metrics"]["heading_accuracy"]["total"] == 4
    assert payload["cases"][0]["got"]["codes"][0]["code"] == PHONE_CODE
    assert payload["meta"]["model"] == "gpt-5.6-terra"


# ---------------------------------------------------------------------------------------
# The regression gate
# ---------------------------------------------------------------------------------------


def _report_of(hits: int, total: int) -> object:
    """`total` classifiable cases, `hits` of them answered with the exactly right code and the
    rest with a code from a different chapter."""
    pairs = []
    for index in range(total):
        case = _case(f"c{index}")
        code = PHONE_CODE if index < hits else LAPTOP_CODE
        pairs.append((case, _obs(case.id, codes=(code,))))
    return build_report(pairs, ReportMeta(generated_at="t", model="gpt-5.6-terra"))


def _baseline_of(**metrics: tuple[int, int]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "metrics": {
            name: {"hits": hits, "total": total, "rate": hits / total}
            for name, (hits, total) in metrics.items()
        },
        "case_ids": [f"c{index}" for index in range(next(iter(metrics.values()))[1])],
        "meta": {"model": "gpt-5.6-terra"},
    }


def test_one_case_flipping_on_a_forty_case_set_is_noise() -> None:
    """The stated rule: a drop of one case's worth of rate is inside the noise floor. At n=40
    and p=0.85 the binomial standard error is ~2.3 cases."""
    report = _report_of(35, 40)
    baseline = _baseline_of(heading_accuracy=(36, 40), exact_code=(36, 40))
    comparison = compare_to_baseline(report, baseline)  # type: ignore[arg-type]
    assert comparison.ok is True
    assert comparison.regressions == []


def test_two_cases_flipping_is_a_regression() -> None:
    report = _report_of(34, 40)
    baseline = _baseline_of(heading_accuracy=(36, 40), exact_code=(36, 40))
    comparison = compare_to_baseline(report, baseline)  # type: ignore[arg-type]
    assert comparison.ok is False
    assert {delta.metric for delta in comparison.regressions} == {
        "heading_accuracy",
        "exact_code",
    }
    assert "tolerance" in comparison.regressions[0].reason


def test_an_improvement_is_never_a_regression() -> None:
    report = _report_of(40, 40)
    comparison = compare_to_baseline(
        report,  # type: ignore[arg-type]
        _baseline_of(heading_accuracy=(30, 40), exact_code=(30, 40)),
    )
    assert comparison.ok is True


def test_the_tolerance_is_wider_on_a_smaller_set() -> None:
    """Ten cases: one flip is ten points and still noise; two is twenty and is not."""
    ok = compare_to_baseline(
        _report_of(8, 10),  # type: ignore[arg-type]
        _baseline_of(heading_accuracy=(9, 10)),
    )
    bad = compare_to_baseline(
        _report_of(7, 10),  # type: ignore[arg-type]
        _baseline_of(heading_accuracy=(9, 10)),
    )
    assert ok.ok is True
    assert bad.ok is False


def test_an_unpopulated_baseline_gates_nothing_but_says_so() -> None:
    comparison = compare_to_baseline(_report_of(20, 40), {})  # type: ignore[arg-type]
    assert comparison.ok is True
    assert any("not populated" in note for note in comparison.notes)


def test_groundedness_below_one_fails_even_with_no_baseline() -> None:
    """No tolerance, ever: the terminal-tool gate makes an ungrounded code impossible, so one
    appearing is a P0 and not a statistical wobble."""
    report = build_report(
        [
            (_case("ok"), _obs("ok")),
            (_case("ghost"), _obs("ghost", status="not_found", is_terminal=False)),
        ]
    )
    comparison = compare_to_baseline(report, {})
    assert comparison.ok is False
    regression = comparison.regressions[0]
    assert regression.metric == "groundedness"
    assert "P0" in regression.reason
    assert "ghost" in regression.reason


def test_over_confidence_regresses_upwards() -> None:
    """Lower is better, so the same one-case tolerance applies in the other direction."""
    vague = [
        parse_case({"id": f"v{i}", "input": VAGUE, "expect_outcome": "clarification"})
        for i in range(10)
    ]
    # Three of ten vague inputs answered with a code, against a baseline of none.
    pairs = [
        (case, _obs(case.id, codes=(PLASTIC_CODE,) if index < 3 else ()))
        for index, case in enumerate(vague)
    ]
    report = build_report(
        [
            (case, obs if obs.codes else _obs(case.id, outcome="clarification", codes=()))
            for case, obs in pairs
        ]
    )
    comparison = compare_to_baseline(report, _baseline_of(over_confidence=(0, 10)))
    assert comparison.ok is False
    assert comparison.regressions[0].metric == "over_confidence"


def test_one_extra_over_confident_case_is_still_noise() -> None:
    vague = [
        parse_case({"id": f"v{i}", "input": VAGUE, "expect_outcome": "clarification"})
        for i in range(10)
    ]
    report = build_report(
        [
            (
                case,
                _obs(case.id, codes=(PLASTIC_CODE,))
                if index == 0
                else _obs(case.id, outcome="clarification", codes=()),
            )
            for index, case in enumerate(vague)
        ]
    )
    comparison = compare_to_baseline(report, _baseline_of(over_confidence=(0, 10)))
    assert comparison.ok is True


def test_a_changed_case_set_is_a_note_and_not_a_failure() -> None:
    """The golden set is expected to grow. Rates stay comparable; per-case flips do not."""
    report = _report_of(40, 44)
    comparison = compare_to_baseline(report, _baseline_of(heading_accuracy=(36, 40)))
    assert comparison.ok is True
    assert any("case set differs" in note for note in comparison.notes)


def test_a_model_swap_is_reported_not_punished() -> None:
    """terra vs luna is one flag, and comparing the two is the point of the gate, not an
    error condition."""
    report = build_report(
        [(_case("c0"), _obs("c0"))],
        ReportMeta(generated_at="t", model="gpt-5.6-luna"),
    )
    comparison = compare_to_baseline(report, _baseline_of(heading_accuracy=(1, 1)))
    assert comparison.ok is True
    assert any("model changed" in note for note in comparison.notes)


def test_a_recorded_baseline_round_trips_through_the_gate(tmp_path) -> None:
    """What `--update-baseline` writes must be exactly what the gate can read back."""
    report = _report_of(38, 40)
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline_from_report(report), indent=2), encoding="utf-8")
    comparison = compare_to_baseline(report, load_baseline(path))
    assert comparison.ok is True
    assert comparison.notes == ()


def test_the_shipped_baseline_is_valid_and_measured() -> None:
    """`evals/baseline.json` was populated by a real 55-case run on 2026-09-16 against
    gpt-5.6-terra. It must parse, carry real metrics, and gate a genuine regression.

    This test originally asserted the baseline was EMPTY — a guard against shipping invented
    numbers before any run existed. That guard did its job; the numbers are now measured, so
    the assertion is inverted: the baseline must NOT be empty, and every rate it carries must
    be a real proportion.
    """
    from pathlib import Path

    baseline = load_baseline(Path(__file__).resolve().parents[1] / "evals" / "baseline.json")
    assert baseline["schema_version"] == 1
    assert baseline["metrics"], "baseline must be populated by a real run"
    for name, entry in baseline["metrics"].items():
        rate = entry["rate"] if isinstance(entry, dict) else entry
        assert 0.0 <= float(rate) <= 1.0, f"{name} is not a proportion: {rate!r}"
    # Groundedness has no tolerance: it is a lookup, so anything below 1.0 is a P0.
    assert float(_rate_of(baseline["metrics"]["groundedness"])) == 1.0
    # A run that matches the baseline exactly must pass.
    assert compare_to_baseline(_report_of(10, 10), baseline) is not None


def _rate_of(entry: object) -> float:
    return float(entry["rate"] if isinstance(entry, dict) else entry)  # type: ignore[index]


# ---------------------------------------------------------------------------------------
# Loading the golden set
# ---------------------------------------------------------------------------------------


def test_a_case_that_expects_clarification_may_not_carry_a_code() -> None:
    """The contradiction that would quietly poison the over-confidence metric: an input cannot
    be both too vague to classify and have a known answer."""
    with pytest.raises(CaseError, match="cannot carry an expected code"):
        parse_case(
            {
                "id": "bad",
                "input": VAGUE,
                "expect_outcome": "clarification",
                "expect_code": PLASTIC_CODE,
            }
        )


def test_an_outcome_is_required_when_no_code_is_expected() -> None:
    with pytest.raises(CaseError, match="expect_outcome"):
        parse_case({"id": "bad", "input": VAGUE})


def test_expect_outcome_defaults_to_result_when_a_code_is_expected() -> None:
    case = parse_case({"id": "c", "input": SMARTPHONE, "expect_code": PHONE_CODE})
    assert case.expect_outcome == "result"
    assert case.expect_heading == "8517"
    assert case.expect_chapter == "85"
    assert case.expects_code is True


def test_code_widths_are_enforced() -> None:
    with pytest.raises(CaseError, match="must be 10 digits"):
        parse_case({"id": "c", "input": SMARTPHONE, "expect_code": "8517"})
    with pytest.raises(CaseError, match="must be 4 digits"):
        parse_case({"id": "c", "input": SMARTPHONE, "expect_heading": "851713"})


def test_id_and_input_are_required() -> None:
    with pytest.raises(CaseError, match="'id' is required"):
        parse_case({"input": SMARTPHONE, "expect_code": PHONE_CODE})
    with pytest.raises(CaseError, match="'input' is required"):
        parse_case({"id": "c", "input": "   ", "expect_code": PHONE_CODE})


def test_tags_accept_one_string_or_a_list() -> None:
    assert parse_case(
        {"id": "c", "input": VAGUE, "tag": "plastics", "expect_outcome": "clarification"}
    ).tags == ("plastics",)
    assert _case(tags=["a", "b"]).tags == ("a", "b")


def test_cases_load_from_json_and_jsonl_in_a_stable_order(tmp_path) -> None:
    (tmp_path / "a_phones.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "phone-1",
                        "input": SMARTPHONE,
                        "expect_code": PHONE_CODE,
                        "tags": ["electronics"],
                    },
                    {"id": "phone-2", "input": SMARTPHONE, "expect_heading": "8517"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "b_vague.jsonl").write_text(
        json.dumps({"id": "vague-1", "input": VAGUE, "expect_outcome": "clarification"}) + "\n",
        encoding="utf-8",
    )
    cases = load_cases(tmp_path)
    assert [case.id for case in cases] == ["phone-1", "phone-2", "vague-1"]
    assert filter_cases(cases, tag="electronics") == [cases[0]]
    assert [case.id for case in filter_cases(cases, limit=2)] == ["phone-1", "phone-2"]


def test_duplicate_case_ids_are_rejected(tmp_path) -> None:
    """Ids are the baseline's join key: two cases sharing one would make the comparison
    meaningless in a way no metric would show."""
    (tmp_path / "a.json").write_text(
        json.dumps([{"id": "dup", "input": SMARTPHONE, "expect_code": PHONE_CODE}]),
        encoding="utf-8",
    )
    (tmp_path / "b.json").write_text(
        json.dumps([{"id": "dup", "input": SMARTPHONE, "expect_code": PHONE_SIBLING}]),
        encoding="utf-8",
    )
    with pytest.raises(CaseError, match="duplicate case id"):
        load_cases(tmp_path)


def test_a_malformed_golden_file_fails_loudly(tmp_path) -> None:
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(CaseError):
        load_cases(tmp_path)


def test_a_missing_cases_path_is_an_error(tmp_path) -> None:
    with pytest.raises(CaseError, match="no such case file"):
        load_cases(tmp_path / "nope")


def test_score_case_assembles_every_verdict() -> None:
    result = score_case(_case(), _obs(codes=(PHONE_SIBLING,)))
    assert (result.exact, result.heading, result.chapter) == (False, True, True)
    assert result.outcome_ok is True
    assert result.over_confidence is False
    assert result.grounded_ok is True
    assert result.answered is True
    assert result.severity == 5


# ---------------------------------------------------------------------------------------
# The harness's two readers. Still offline: no API key, no database, no model.
#
# These are the pieces of `evals/harness.py` that are coupled to somebody else's code —
# `respond()`'s turn summary and the shape of the turn ledger — so they are the pieces most
# likely to rot silently. Everything else in the harness needs Postgres and the live API and
# belongs to the parent's live run.
# ---------------------------------------------------------------------------------------


def _summary_line(**overrides: object) -> str:
    """The exact line `app/chat/server.py` logs at the end of every turn."""
    fields: dict[str, object] = {
        "user": 1,
        "thread": "thr_eval_abc",
        "outcome": "result",
        "error": None,
        "model": "gpt-5.6-terra",
        "prompt": "p2.2026-09-16/abc123def456",
        "turns": 4,
        "repairs": 1,
        "tools": 5,
        "codes_seen": 12,
        "in": 4321,
        "cached": 1000,
        "out": 512,
        "ttfb_ms": 280,
        "latency_ms": 7400,
    }
    fields.update(overrides)
    return "turn done " + " ".join(f"{key}={value}" for key, value in fields.items())


def test_the_turn_summary_is_captured_and_keyed_by_thread() -> None:
    """Usage is stale until the stream drains, so `respond()` logs it in `finally` and the
    harness reads it from there. Concurrency makes the `thread=` key load-bearing."""
    import logging

    from evals.harness import _TurnLog

    handler = _TurnLog()
    logger = logging.getLogger("uktzed.chat.test")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info(_summary_line(thread="thr_a", outcome="result"))
        logger.info(_summary_line(thread="thr_b", outcome="clarification"))
        logger.info("some other line that is not a turn summary")
    finally:
        logger.removeHandler(handler)

    assert handler.take("thr_b")["outcome"] == "clarification"
    assert handler.take("thr_a")["turns"] == "4"
    assert handler.take("thr_a") == {}  # taken once; a second read is empty, not stale


def test_missing_fields_read_as_none_rather_than_raising() -> None:
    """A field that moves or disappears must cost a token count, never the whole run."""
    from evals.harness import _int, _text

    line = _summary_line(turns="None").removeprefix("turn done ")
    fields = dict(item.split("=", 1) for item in line.split())
    assert _int(fields, "turns") is None
    assert _int(fields, "nope") is None
    assert _text(fields, "error") is None
    assert _text(fields, "outcome") == "result"


def test_the_server_still_logs_every_field_the_harness_reads() -> None:
    """The coupling, pinned. If somebody renames a key in the turn summary, this fails here
    and names the harness, instead of quietly zeroing a column in the eval report."""
    import inspect

    from app.chat.server import ClassifierServer

    source = inspect.getsource(ClassifierServer.respond)
    for key in (
        "turn done",
        "thread=",
        "outcome=",
        "error=",
        "model=",
        "prompt=",
        "turns=",
        "repairs=",
        "codes_seen=",
        "in=",
        "cached=",
        "out=",
        "ttfb_ms=",
        "latency_ms=",
    ):
        assert key in source, f"evals/harness.py parses {key!r} out of the turn summary"


def _ledger_with_a_repair():
    """A ledger shaped like a real turn: navigate, get rejected, repair, succeed."""
    from app.agent.provenance import TurnLedger

    ledger = TurnLedger()
    walk = ledger.begin_call("open_category", {"category_code": "8517"})
    ledger.end_call(walk, digest="CategoryTree:2")
    rejected = ledger.begin_call("emit_classification", {"codes": ["0000000000"]})
    ledger.end_call(rejected, error="code_not_in_dataset")
    accepted = ledger.begin_call("emit_classification", {"codes": ["8517 13 00 00"]})
    ledger.end_call(accepted, digest="ok:1:mismatch=0")
    return ledger


def test_emitted_codes_come_from_the_accepted_call_only() -> None:
    """A rejected `emit_classification` showed nobody anything: the repair is the answer.
    Codes are normalised on the way out, because the model may space them."""
    from evals.harness import _emitted_codes, _outcome

    ledger = _ledger_with_a_repair()
    assert _emitted_codes(ledger) == (PHONE_CODE,)
    assert _outcome(ledger) == "result"


def test_the_outcome_falls_back_to_the_ledger() -> None:
    """If the turn summary never arrived, the outcome is still derivable the way the product
    decides it: by which terminal tool succeeded."""
    from app.agent.provenance import TurnLedger
    from evals.harness import _emitted_codes, _outcome

    ledger = TurnLedger()
    asked = ledger.begin_call("ask_clarification", {"options": 3})
    ledger.end_call(asked, digest="clarification:3")
    assert _outcome(ledger) == "clarification"
    assert _emitted_codes(ledger) == ()

    empty = TurnLedger()
    assert _outcome(empty) == "conversation"


async def test_the_observation_assembles_the_ledger_and_the_summary() -> None:
    """`_observe` end to end against a stub tariff: the groundedness lookup runs through the
    real `validate_and_resolve`, and the cost comes from the real price table."""
    from app.agent.schemas import NodeDetail
    from evals.harness import Harness, HarnessOptions, _TurnLog

    class _Repo:
        """Enough of `TariffRepo` for `validate_and_resolve`: it only calls `resolve`."""

        async def resolve(self, code: str) -> NodeDetail | None:
            if code != PHONE_CODE:
                return None
            return NodeDetail(
                code=code,
                level="code",
                description="смартфони",
                full_path="XVI > 85 > 8517 > смартфони",
                is_terminal=True,
                depth=4,
                child_count=0,
                ancestor_codes=["16", "85", "8517"],
                path_is_ambiguous=False,
                is_dead_end=False,
            )

    harness = Harness(
        server=None,  # type: ignore[arg-type]
        store=None,  # type: ignore[arg-type]
        repo=_Repo(),  # type: ignore[arg-type]
        turn_log=_TurnLog(),
        options=HarnessOptions(),
    )
    fields = dict(item.split("=", 1) for item in _summary_line().removeprefix("turn done ").split())
    observation = await harness._observe(
        case=_case("phone-001"),
        fields=fields,
        ledger=_ledger_with_a_repair(),
        wall_ms=7600,
        failure=None,
    )

    assert observation.outcome == "result"
    assert observation.code_list == (PHONE_CODE,)
    assert observation.codes[0].status == "valid"
    assert grounded(observation) is True
    assert (observation.turns, observation.repairs) == (4, 1)
    assert len(observation.tool_calls) == 3
    assert observation.latency_ms == 7400  # the product's own number, not the wall clock
    assert observation.wall_ms == 7600
    assert observation.model == "gpt-5.6-terra"
    assert observation.prompt_version == "p2.2026-09-16"
    # 3,321 fresh input at $2/Mtok + 1,000 cached at $0.20 + 512 output at $12 = $0.012986.
    assert observation.cost_usd == Decimal("0.012986")


async def test_a_failed_turn_is_recorded_as_an_error_outcome() -> None:
    """Mirrors `fail_turn`: the record stores a failed turn as `error`, so the eval does too.
    A case that fails is a measurement, not a crash."""
    from evals.harness import Harness, HarnessOptions, _TurnLog

    harness = Harness(
        server=None,  # type: ignore[arg-type]
        store=None,  # type: ignore[arg-type]
        repo=None,  # type: ignore[arg-type]
        turn_log=_TurnLog(),
        options=HarnessOptions(),
    )
    observation = await harness._observe(
        case=_case(),
        fields={},  # the summary never arrived: the turn died early
        ledger=_ledger_with_a_repair(),
        wall_ms=1200,
        failure=TimeoutError("turn exceeded 180.0s"),
    )
    assert observation.outcome == "error"
    assert observation.error_class == "timeout"  # the closed vocabulary, via classify_error
    assert observation.latency_ms == 1200  # no summary, so the wall clock stands in


async def test_run_many_is_bounded_and_keeps_the_order_it_was_given() -> None:
    """A 50-case run is minutes because cases overlap, and the bound is what keeps the eval
    from measuring the provider's rate limiter instead of the classifier."""
    import asyncio

    from evals.harness import Harness, HarnessOptions, _TurnLog

    class _Probe(Harness):
        """Replaces the one method that talks to the model; the scheduling is the real one."""

        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            self.in_flight = 0
            self.peak = 0

        async def _run(self, case: Case) -> Observation:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            await asyncio.sleep(0.005)
            self.in_flight -= 1
            return Observation(case_id=case.id)

    cases = [_case(f"c{index:02d}") for index in range(12)]
    probe = _Probe(
        server=None,
        store=None,
        repo=None,
        turn_log=_TurnLog(),
        options=HarnessOptions(concurrency=3),
    )
    finished: list[str] = []
    observations = await probe.run_many(cases, on_done=lambda case, _: finished.append(case.id))

    assert probe.peak == 3
    assert [obs.case_id for obs in observations] == [case.id for case in cases]
    assert sorted(finished) == sorted(case.id for case in cases)


def test_the_shipped_golden_set_parses() -> None:
    """The golden set and the loader are written in different places; this is the seam.

    Skipped rather than failed when `evals/golden/` is empty, because the set arrives in its
    own commit — but the moment a case file exists, it has to load, and every id has to be
    unique across every file.
    """
    from pathlib import Path

    cases = load_cases(Path(__file__).resolve().parents[1] / "evals" / "golden")
    if not cases:
        pytest.skip("evals/golden/ is empty; the golden set lands in its own commit")

    assert len({case.id for case in cases}) == len(cases)
    for case in cases:
        assert case.input.strip()
        assert case.expect_outcome in {"result", "clarification", "conversation"}
        if case.expect_code:
            assert len(case.expect_code) == 10
        if case.expect_outcome != "result":
            assert case.expect_code is None


def test_a_yaml_directory_is_loaded_through_the_golden_set_builder(tmp_path) -> None:
    """`evals/README.md` makes `build_golden.py` the single implementation of the schema, so
    the loader here defers to it for YAML and converts, rather than re-parsing the files."""
    pytest.importorskip("yaml")
    (tmp_path / "cases.yaml").write_text(
        "- id: yaml-case-1\n"
        f'  input: "{SMARTPHONE}"\n'
        "  expect_outcome: result\n"
        f'  expect_code: "{PHONE_CODE}"\n'
        "  rationale: >-\n"
        "    Heading 8517 covers telephones for cellular networks, and 8517 13 names\n"
        "    smartphones outright, so the sibling 8517 14 is excluded by its own text.\n"
        "  source: domain-reasoning\n"
        "  tags: [electronics]\n",
        encoding="utf-8",
    )
    cases = load_cases(tmp_path)
    assert [case.id for case in cases] == ["yaml-case-1"]
    assert cases[0].expect_code == PHONE_CODE
    assert cases[0].expect_heading == "8517"
    assert cases[0].source == "domain-reasoning"  # the case's provenance, kept
    assert cases[0].tags == ("electronics",)
    assert "8517 13" in cases[0].note  # `rationale` lands in `note`


def test_the_strict_golden_schema_is_enforced_on_yaml(tmp_path) -> None:
    """A typo'd field name must fail the run, not silently change a denominator. That check
    lives in the golden-set builder, which is exactly why YAML goes through it."""
    pytest.importorskip("yaml")
    (tmp_path / "cases.yaml").write_text(
        "- id: yaml-case-2\n"
        f'  input: "{SMARTPHONE}"\n'
        "  expect_outcome: result\n"
        f'  expect_code: "{PHONE_CODE}"\n'
        "  rationale: >-\n"
        "    A rationale long enough to count as a justification rather than a label here.\n"
        "  source: domain-reasoning\n"
        "  tags: [electronics]\n"
        "  expct_headng: '8517'\n",  # the typo
        encoding="utf-8",
    )
    with pytest.raises(CaseError, match="unknown field"):
        load_cases(tmp_path)


def test_a_golden_case_object_converts_without_losing_anything() -> None:
    """The adapter is duck-typed, so a field added to `GoldenCase` cannot break a run here."""
    from dataclasses import dataclass as _dataclass

    from evals.scoring import case_from_golden

    @_dataclass
    class _Golden:
        id: str = "g-1"
        input: str = SMARTPHONE
        expect_outcome: str = "result"
        expect_code: str | None = PHONE_CODE
        expect_heading: str | None = None
        accept_codes: tuple[str, ...] = (PHONE_SIBLING,)
        rationale: str = "why"
        source: str = "verified-live"
        tags: tuple[str, ...] = ("electronics",)
        source_file: str = "verified_live.yaml"

    case = case_from_golden(_Golden())
    assert case.id == "g-1"
    assert case.accepted_codes == (PHONE_CODE, PHONE_SIBLING)
    assert case.expect_chapter == "85"
    assert case.location == "verified_live.yaml"
    assert case.note == "why"
