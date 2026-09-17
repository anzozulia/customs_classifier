# terra vs luna — measured, 2026-09-17

The architecture left this open ("do not pick luna because it is the SDK default — let the
golden set answer it"). Both runs are the same 55 cases, same prompt `p2.2026-09-16`, same
dataset `5a113fc09ac0`, same harness.

| | gpt-5.6-terra @ low | gpt-5.6-luna @ medium |
|---|---|---|
| **exact_code** | 43/43 · **100%** | 44/44 · **100%** |
| heading_accuracy | 44/44 · 100% | 45/45 · 100% |
| chapter_accuracy | 44/44 · 100% | 45/45 · 100% |
| answer_rate | 44/45 · 97.8% | 45/45 · **100%** |
| outcome_match | 53/55 · **96.4%** | 50/55 · 90.9% |
| over_confidence | 1/8 · 12.5% | 1/8 · 12.5% |
| groundedness | 100% | 100% |
| error_rate | 0/55 · **0%** | 1/55 · 1.8% |
| latency mean / p90 | 10.5s / 15.1s | 11.1s / 15.3s |
| **cost, 55 cases** | **$2.30** | **$0.23** |
| cost mean / p90 | $0.0417 / $0.0480 | $0.0042 / $0.0054 |

## What this says

**Accuracy is not the differentiator.** Both models get every code they commit to right, and
both are perfectly grounded. On this set, paying 10x more buys no additional correctness.

**The difference is temperament.** luna ANSWERS more (answer_rate 100% vs 97.8%) and clarifies
less, which is why its outcome_match is lower — it committed on three cases the golden set says
should have been questions. That is not the same as being wrong: over_confidence, the metric
that counts emitting a code on a genuinely vague input, is identical at 12.5%. But for customs
work "asks when unsure" is a feature, and terra does more of it.

luna also produced the one `internal` error across both runs.

## Recommendation

**Run the public demo on luna.** A 10x cost reduction with identical exact-code accuracy is
exactly the lever for an open demo, and the failure mode (answers instead of asking) is mild.
Switch to terra for private/considered use where the clarification instinct is worth paying for.

Both are one click apart in the back office, so this is reversible mid-demo.

## Caveats

- 55 cases. A 3-case swing in outcome_match is within noise of a set this size; the COST
  difference is not.
- Tariff-derived cases are written from the answer, so they are easier than real user input.
- luna was run at `medium` effort and terra at `low` — a deliberate pairing (the cheap model
  gets more thinking), not a controlled single-variable comparison.
