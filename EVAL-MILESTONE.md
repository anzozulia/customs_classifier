# Milestone: the evaluation harness

> v1 had no evaluation at all. `test_agents.py` called eight test functions that were never
> defined, so it crashed on import in every commit. 1,403 production classifications produced
> zero measured accuracy — and that is exactly why exactly **one** prompt line changed in ten
> commits. With nothing to measure, every change is a coin flip nobody can call, so the
> rational move is to change nothing.

`evals/` was an empty directory. This milestone fills it with a golden set, a harness that
runs it through the real product, a scorer that says what "better" means, and a gate that
fails the build when a number moves the wrong way.

---

## What is there

| file | job |
|---|---|
| `evals/golden/*.yaml` | 55 cases across 8 theme files, 21/21 tariff sections |
| `evals/build_golden.py` | the schema, the loader, and the **offline** ground-truth verifier |
| `evals/harness.py` | runs one case through `ClassifierServer.respond()` — the real agent |
| `evals/scoring.py` | the metrics, as pure functions, plus the report and the regression gate |
| `evals/run.py` | the CLI and the exit-code contract |
| `evals/baseline.json` | the recorded scorecard — **a placeholder, every number `null`** |

`evals/README.md` is the golden set's own document: the schema, the provenance of every case,
and its limits case by case. This file is the milestone-level one.

---

## The metrics, and why heading-level leads

A turn ends in exactly one of three ways, decided by which terminal tool the agent called:
`emit_classification` → `result`, `ask_clarification` → `clarification`, neither →
`conversation`. **A clarification is not a failure.** In v1 production 17.4% of turns were
clarifications and the loop is part of the product. An eval that scores a clarification as a
wrong answer optimises the system into confident guessing, and a wrong customs code has legal
and financial consequences — it is the single worst outcome this system can produce.

So the scorecard refuses to blur two different questions into one number.

### Was the answer right? (conditional accuracy)

Scored **only** over cases that expect a code *and* on which the system actually emitted one.
A clarification leaves these three alone entirely — it is not a miss, it is not a hit, it is
not in the denominator.

| metric | question |
|---|---|
| **`heading_accuracy`** | do the first **4** digits match? |
| `exact_code` | does the full 10-digit code match `expect_code` or an `accept_codes` entry? |
| `chapter_accuracy` | do the first **2** digits match? |

**`heading_accuracy` leads the report**, and that is a deliberate choice about what a customs
classifier is for. A heading-level hit means the agent found the right *product family* and
missed on a subdivision — a near miss a human finishes in seconds by reading four sibling
leaves. A wrong chapter is not a near miss; it is a different product, and no amount of human
review recovers from it cheaply. `exact_code` is the stricter number underneath and
`chapter_accuracy` the coarse sanity floor, but the number that tracks *useful* is the middle
one.

It is also the number this dataset can honestly support. 2,487 of 10,490 terminals (23.7%)
share a `full_path` with a sibling because the snapshot dropped the header text that separates
them; on those, the last six digits are a coin flip for a human too. Leading with `exact_code`
would report the snapshot's ambiguity as the model's error.

All three are pure string prefix work — the first 4 digits of a 10-digit code **are** its
heading, the first 2 **are** its chapter. No lookup, nothing to get out of sync.

### Did it answer when it should have?

`answer_rate` — of the cases the golden set says *are* classifiable, how many got a code.
Denominator: 48 cases.

This is the metric that makes the conditional denominator safe. Without it, "ask for
clarification on everything" scores 100% accuracy on the two cases it deigns to answer. With
it, that strategy posts a catastrophic `answer_rate` and the gate fails it. `answer_rate` and
the accuracy metrics are gated **together**, on purpose: neither means anything alone.

`outcome_match` is the same question over all 55 cases, including the 7 that expect no code.

### Did it answer when it should **not** have?

`over_confidence` — it emitted a code on an input the golden set marks too vague to classify.
Denominator: the 5 `clarification` cases. Lower is better, and it gets its own block in the
report rather than a line in a table.

Two details matter. It is measured on **emitting a code**, not on the outcome label — the harm
is the wrong code reaching a user, not the bookkeeping. And its denominator is small on
purpose: it is a tripwire, not a statistic. One case flipping here is worth investigating even
though the same flip elsewhere is noise.

### Is the code real?

`groundedness` — does every emitted code exist in the tariff and is it terminal? A **lookup**
through `validate_and_resolve`, the same SQL function the terminal-tool gate uses. Never an
LLM judge.

This should read 100% by construction, because `emit_classification` refuses anything else.
It is measured anyway, because "should be impossible" is a claim and this is the file where
claims get checked. Below 1.0 it is a **P0** with the case named, and it fails the gate with
or without a baseline — no tolerance, no noise argument. A lookup that itself fails is
recorded as `lookup_failed` and counts as ungrounded, so the failure mode is loud rather than
silently passing.

### Efficiency

mean / p50 / p90 of turns, tool calls, latency and cost, from the product's own
instrumentation. Percentiles are nearest-rank with no interpolation, so every number printed
is one a run actually produced. Money is `Decimal` end to end and crosses JSON as a **string**
— matching `classification.cost_usd NUMERIC(10,6)`. Do not parse those as floats.

### The regression rule

> A higher-is-better metric regresses only when it drops by **more than one case's worth of
> rate**, computed on the smaller of the two denominators.

One case flipping on a 40-case set is noise: at n=40, p=0.85 the binomial standard error is
about 2.3 cases. Smaller sets get a proportionally more permissive tolerance, which is the
right direction for a gate whose false alarm costs a human an afternoon.
`over_confidence` uses the same tolerance reversed. `groundedness` has none.

An unpopulated baseline gates nothing except the groundedness floor. A changed case set or a
model swap is a **printed note, never a failure** — comparing terra against luna is the point
of the `--model` flag, not an error.

---

## Honest limits

`evals/README.md` carries eight of these case by case. The four that change how you should
read a number:

**1. The descriptions were written from the answer.** 37 of 55 cases are `tariff-derived`: a
terminal leaf was chosen by walking the real JSON, and the Ukrainian description was then
written *from* its `full_path`. So the vocabulary already matches the tariff's own wording and
the retrieval problem is half solved before the agent starts. Real users write «ноутбук»; the
tariff writes «машини обчислювальні портативні», and `ILIKE '%ноутбук%'` matches zero rows in
this dataset. **These cases measure whether navigation reaches the right leaf. They do not
measure whether the system can bridge consumer language to tariff language** — the harder half,
and the one embeddings are meant to close. Expect this set to score well above real traffic.
Do not quote it as production accuracy.

**2. 55 cases is a smoke test, not a statistic.** One case is 1.8 points. A three-case swing
between runs is noise. Read the per-tag and per-section breakdowns to see *where* something
moved before believing anything moved.

**3. The set is systematically easier than the tariff.** No case expects a residual «інші»
leaf (2,473 of 10,490 terminals) or an ambiguous one (2,487). Both are everywhere in real
traffic. The verifier warns if you add one.

**4. Some ground truth is defensible, not certain.** Two cases carry `accept_codes` because
the description genuinely cannot decide: `adv-rubber-boots` cannot be split between 6401 and
6402 without knowing how the upper is attached, `cons-hard-hat` cannot be split between
6506 10 10 and 6506 10 80 without the material.

There is also a gap in the snapshot itself, which is not the set's doing: heading **8712**
(велосипеди) is absent entirely, so a pedal bicycle has nowhere correct to go and no case
tests one.

### Why the verifier exists, in one line

The brief for the golden set proposed `accept_codes: ["3919101100"]` for the film case. That
code **does not exist in this dataset** and `build_golden.py` rejects it. A plausible,
good-faith, domain-informed neighbouring code, written by someone who knows the domain — and
simply not real. Every code in the set is checked against `data/uktzed_hierarchical.json`
through `app.tariff.ingest.prepare`, the same function the Postgres ingest uses, so "terminal"
here means exactly what it will mean in the database.

---

## How to run it

```
make eval-verify   # offline ground truth — no API key, no database, no docker
make eval-fast     # 8-case pre-commit subset through the real agent
make eval          # all 55 cases, gated against evals/baseline.json
```

`eval` and `eval-fast` run `eval-verify` first: burning live API calls on a golden file with a
typo in it is the expensive way to find the typo.

**`make eval-verify` is the one that belongs in CI.** It needs nothing — verified by running
it under `env -i` with no `OPENAI_API_KEY`, no `DATABASE_URL` and no `PATH` beyond the venv.
It checks that every expected code exists and is terminal, that every heading exists, that ids
are unique, and prints the distribution by file, outcome, source, tag, section and granularity.
Exit 0 on PASS, 1 on any failure.

**`make eval-fast`** defaults to `evals/golden/behaviour.yaml` — 8 cases, and the only theme
file that covers all three outcomes (2 clarification, 2 conversation, 4 result), so
`over_confidence` and `answer_rate` both have a real denominator on it. It also holds both
threshold **pairs**: bottle 1,5 l → `3923301000` against canister 5 l → `3923309000`, and
motorcycle 400 cm³ → `8711309000` against 650 cm³ → `8711400000`. A model that reads the
product and ignores the measurement gets exactly one of each pair right — invisible in an
aggregate, obvious here. Override with `make eval-fast EVAL_CASES=evals/golden/adversarial.yaml`.

Beneath the Makefile it is one CLI:

```
python -m evals.run --dry-run                     # parse and print the cases; no API, no DB
python -m evals.run --tag name-vs-material        # one tag
python -m evals.run --model gpt-5.6-luna --json luna.json
python -m evals.run --update-baseline             # record, do not compare
```

Exit codes are the contract: **0** clean, **1** a regression, **2** could not run at all (no
cases, no API key, a malformed golden file, a dead database).

Two flags that matter before a live run. `--user-id` defaults to `1` and is a foreign key to
`app_user`; if that user does not exist the `classification` row write is skipped (the writer
swallows its own failures), the case still runs and still scores, but the eval turns never land
in the metrics table. Pass a real user id. `--model` is process-wide — it sets `MODEL` and
clears the settings cache before the first turn — so one model per process, and terra vs luna
is two runs.

---

## What is verified, offline, right now

Everything below is real output from this machine, not a description of what should happen.

**`.venv/bin/python evals/build_golden.py` → exit 0.** 55 cases in 8 files; 48 `result`,
5 `clarification`, 2 `conversation`; 46 at 10-digit precision, 2 at 4-digit heading, 7
outcome-only; 21/21 sections covered; 2 warnings, both deliberate (a code deliberately reused
by an adversarial twin). Final line:

```
PASS — 48 code(s) exist and are terminal, 2 heading(s) exist, 55 id(s) unique.
```

**The two chunks are reconciled.** `evals.scoring.load_cases()` delegates a YAML directory to
`evals.build_golden.load_cases()` rather than re-parsing the files, so there is exactly one
implementation of the schema. Checked field by field over all 55 real cases: `input`,
`expect_outcome`, `expect_code`, `accept_codes`, `tags`, `source` and `rationale`→`note` are
identical on both sides, and `all_codes` matches `accepted_codes` in order. `expect_heading` is
the one field that differs by design — explicit on the golden side, and on the scoring side
also *derived* as `expect_code[:4]` for the 46 cases that carry a code, which is what makes
`heading_accuracy` scorable for all 48 classifiable cases rather than only the 2 heading-only
ones. Both optional fields were confirmed to change a score, not merely survive the trip:
a heading-only case scores `exact_code` as N/A and `heading_hit` as a hit; an `accept_codes`
alternate scores as exactly right; a clarification on a vague case scores N/A on accuracy and
`False` on `over_confidence`, while a code on that same case scores `True`.

**`ruff check app tests scripts migrations evals`** → `All checks passed!`

**`pytest -q`** → **183 passed** (116 before this milestone, 67 new).

**`python -c "import evals.run, evals.harness, evals.scoring"`** → imports clean.

**`pyyaml==6.0.3`** is declared in `[dependency-groups] dev` and matches what is installed
(`pip show pyyaml` → 6.0.3, a real `cp313-macosx_11_0_arm64` wheel from a package index). It
was previously an undeclared transitive accident — `Required-by` is empty — that a clean
install would have dropped, taking the golden set with it.

### One thing that is not green, and predates this milestone

`make lint` runs `ruff check . && ruff format --check .`. The **check** half passes. The
**format** half does not, on 13 files: eleven of them (`app/agent/agent.py`,
`app/agent/tools_nav.py`, `app/agent/tools_terminal.py`, `app/chat/routes.py`,
`app/tariff/catalogue.py`, `app/tariff/ingest.py`, `app/tariff/validate.py`,
`migrations/versions/0003_tariff.py`, `scripts/ingest.py`, `scripts/spike_m0.py`,
`tests/test_ingest_invariants.py`) predate this milestone, and two are new here
(`evals/build_golden.py`, `evals/README.md` — ruff formats fenced Python in Markdown). Every
difference is cosmetic line-joining. It was left alone rather than swept into this milestone's
diff, but `make lint` is red and was already red.

---

## What a live run still has to establish

Nothing in this milestone has touched the live API or a database. **Every accuracy number in
`evals/baseline.json` is `null`**, deliberately — an invented baseline makes the gate lie in
both directions. Until a real run populates it, the gate enforces only the absolute
groundedness floor.

A live run has to answer these, in roughly this order:

1. **Does the harness actually work end to end?** Everything above is offline. The pieces that
   have never executed are the ones that touch Postgres and the API: the pool, the real
   `TariffRepo`, `ClassifierServer.respond()` under concurrency, and the groundedness lookup.
   Start with `make eval-fast` — 8 cases — before spending 55.
2. **Does the turn-summary parse hold?** The harness reads outcome, turns, repairs, tokens and
   latency off the `uktzed.chat` "turn done …" INFO line, keyed by `thread=`. A test asserts
   via `inspect.getsource` that `app/chat/server.py` still logs every key, so a rename fails
   loudly — but the parse itself has only been exercised against a synthetic line.
3. **Is `groundedness` actually 1.0?** It is 1.0 by construction if the terminal-tool gate has
   no hole. This is the first time that claim is tested against the real dataset over 48
   emitted codes. Anything below 1.0 is a P0 in the gate, not in the eval.
4. **What are the real numbers?** `heading_accuracy` first, then the gap between it and
   `exact_code` — a wide gap means the agent finds the right family and loses the subdivision,
   which is a prompt problem; a narrow gap at a low level means navigation itself is failing.
5. **Do the threshold pairs split?** Four cases, two pairs, one number's difference in the
   wording. Getting exactly one of each pair right is the specific, diagnosable failure that
   an aggregate would hide.
6. **What does `over_confidence` read on 5 vague cases?** Non-zero is the finding that should
   stop everything else, because it is the failure mode with legal consequences.
7. **What does a run cost, and how long does it take?** Nothing in this repo has ever measured
   the cost of 55 turns. That number decides whether `make eval` is something you run per PR
   or per release.
8. **Then, and only then: `--update-baseline`.** Commit the baseline together with the change
   that produced it, and say in the commit message why the numbers are what they are.

Two questions the harness is built to answer but nobody has asked it yet: **terra vs luna**
(two runs, `--model`, compare the reports — the comparison prints a note rather than failing,
because that comparison is the point), and whether any of the 55 expectations is simply wrong.
On a set this small, one disputed case is 1.8 points, and every case carries a `rationale`
arguing it against its neighbours precisely so that argument can be had.
