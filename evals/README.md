# The golden set

55 classification cases with defensible ground truth, one YAML file per theme under
`golden/`, and one script that refuses to let a made-up code into the set.

This exists because of the single deepest flaw in v1: **v1 had no evaluation at all.** Its
`test_agents.py` called eight test functions that were never defined, so it crashed on
import in every commit. 1,403 production classifications produced zero measured accuracy —
and that is exactly why, across ten commits, precisely one line of the prompt ever changed.
You cannot tune what you cannot measure, so nobody tried.

A golden set with invented ground truth would be worse than that. It would produce a
number, the number would look like accuracy, and it would measure nothing. So the rule
here is absolute:

> **Every expected code is looked up in the real tariff and has to come back existing and
> terminal, or `build_golden.py` exits non-zero.**

---

## Run the verifier

```bash
.venv/bin/python evals/build_golden.py
```

Offline — it reads `data/uktzed_hierarchical.json` and touches neither Postgres nor the
OpenAI API. It needs `pyyaml`, which is declared in the `dev` dependency group (it was
present in the venv only as an undeclared transitive package before this milestone).

Real output, on the set as committed:

```text
UKTZED golden set — verification report
========================================================================
tariff      data/uktzed_hierarchical.json
sha256      5a113fc09ac05028afbd6ed1884a95133b65e7827e61a87d74d5ffb11a38dee3
nodes       14,187   terminals 10,490

cases       55 in 8 file(s)

by file
  adversarial.yaml                6  ##############
  behaviour.yaml                  8  ###################
  consumer_goods.yaml            10  ########################
  food_and_agriculture.yaml       7  #################
  machinery_and_transport.yaml    8  ###################
  materials_and_chemicals.yaml    8  ###################
  v1_prompt_examples.yaml         5  ############
  verified_live.yaml              3  #######

by outcome
  result          48  ########################
  clarification    5  ##
  conversation     2  #

by source
  verified-live        3  ##
  tariff-derived      37  ########################
  v1-prompt-example    5  ###
  domain-reasoning    10  ######

by tag
  ambiguous-leaf        1  ###
  art                   1  ###
  beverages             3  ########
  brand-name            3  ########
  chemicals             3  ########
  coarse-expectation    2  #####
  construction          2  #####
  dimension-rule        9  ########################
  electronics           7  ###################
  food                  4  ###########
  footwear              2  #####
  glass-ceramics        1  ###
  household             2  #####
  instruments           4  ###########
  jewellery             1  ###
  known-ambiguous       1  ###
  leather               1  ###
  lighting              1  ###
  metals                1  ###
  multi-product         1  ###
  name-vs-material      9  ########################
  non-terminal-echo     2  #####
  paper                 1  ###
  plastics              5  #############
  system-question       2  #####
  textiles              4  ###########
  vague-input           5  #############
  vehicles              4  ###########
  weapons               2  #####
  wood                  3  ########

by tariff section  (21/21 covered)
  01    1  Живі тварини; продукти тваринного походження
  02    2  Продукти рослинного походження
  03    1  Жири та олії тваринного, рослинного або мікробного поход
  04    3  Готові харчові продукти; алкогольні та безалкогольні нап
  05    1  Мінеральні продукти
  06    2  Продукція хімічної та пов’язаних із нею галузей промисло
  07    5  Полімерні матеріали, пластмаси та вироби з них; каучук, 
  08    1  Шкури необроблені, шкіра вичинена, натуральне та штучне 
  09    3  Деревина і вироби з деревини; деревне вугілля; корок та 
  10    1  Маса з деревини або з інших волокнистих целюлозних матер
  11    2  Текстильні матеріали та текстильні вироби
  12    4  Взуття, головні убори, парасольки від дощу та сонця, пал
  13    1  Вироби з каменю, гіпсу, цементу, азбесту, слюди або анал
  14    1  Перли природні або культивовані, дорогоцінне або напівдо
  15    1  Недорогоцінні метали та вироби з них
  16    7  Машини, обладнання та механізми; електротехнічне обладна
  17    3  Засоби наземного транспорту, літальні апарати, плавучі з
  18    4  Прилади та апарати оптичні, фотографічні, кінематографіч
  19    2  Різні промислові товари
  20    2  Різні промислові товари
  21    1  Твори мистецтва, предмети колекціонування та антикваріат
  --     7  (no code expected: clarification / conversation)

by expectation granularity
  10-digit          46  ########################
  4-digit heading    2  #
  outcome only       7  ####

warnings (2)
  ! mach-laptop (machinery_and_transport.yaml): code 8471300000 is also expected by adv-nonterminal-8471
  ! live-plastics-film (verified_live.yaml): code 3919101200 is also expected by adv-nonterminal-3919

PASS — 48 code(s) exist and are terminal, 2 heading(s) exist, 55 id(s) unique.
```

The two warnings are deliberate: `adv-nonterminal-3919` and `adv-nonterminal-8471`
re-use the goods of `live-plastics-film` and `mach-laptop` on purpose, so that the only
variable between the pair is the non-terminal code the user asserts. Warnings never fail
the run.

---

## The case format

One YAML file per theme, each a plain top-level **list** of case mappings. A file's leading
comment says what the theme is and how its ground truth was established.

```yaml
- id: live-plastics-film                       # unique across ALL files, lowercase kebab
  input: "Самоклейна плівка з полівінілхлориду у рулоні завширшки 15 см"
  expect_outcome: result                       # result | clarification | conversation
  expect_code: "3919101200"                    # 10 digits; only when outcome is `result`
  accept_codes: ["6402991000"]                 # optional defensible alternatives
  expect_heading: "5603"                       # optional; 4 digits; INSTEAD of expect_code
  rationale: >-                                # why this code and not its neighbours
    Heading 3919 splits first on width ...
  source: verified-live                        # where the CASE came from
  tags: [plastics, dimension-rule]             # non-empty, lowercase kebab
```

| field | required | meaning |
| --- | --- | --- |
| `id` | yes | Unique across every file. Lowercase kebab-case. Stable: the baseline is keyed on it, so renaming an id loses its history. |
| `input` | yes | The user turn, verbatim, in Ukrainian. |
| `expect_outcome` | yes | Which terminal tool the turn must end on. |
| `expect_code` | result only | The declarable 10-digit code. Verified to exist and be terminal. |
| `accept_codes` | no | Alternatives that are genuinely defensible from the same description. Not counted wrong. Each is verified exactly like `expect_code`. Needs an `expect_code` to be an alternative *to*. |
| `expect_heading` | result only | A 4-digit heading, used **instead of** `expect_code` where a 10-digit answer would be a coin flip. Scored at heading level: compare the first 4 digits of whatever the system emitted. |
| `rationale` | yes | At least 40 characters. Must argue against the neighbouring leaves, not just restate the answer. |
| `source` | yes | One of the four labels below. |
| `tags` | yes | Non-empty. Drives the per-tag breakdown, which is how you see whether a regression is concentrated (e.g. all `dimension-rule` cases) or spread. |

`expect_code` and `expect_heading` are mutually exclusive, and a `result` case must carry
one of them — otherwise it asserts nothing about the code and only looks like coverage.
A `clarification` or `conversation` case must carry neither.

### Scoring, and the one thing not to get wrong

**A clarification is not a failure.** In v1 production 17.4% of turns ended in
`ask_clarification`, and the loop is part of the product. An eval that scores a
clarification as a miss will push the system toward confident wrong answers — the single
worst outcome in customs classification, where a wrong code carries legal and financial
consequences. The five `clarification` and two `conversation` cases here are scored as
strictly as the 48 code cases: producing a code where the set expects a question is a miss,
exactly like producing the wrong code.

Prefix scoring is free. The first 2 digits of a 10-digit code **are** its group and the
first 4 **are** its category, so group- and heading-level accuracy are pure string slicing
with no lookup.

---

## The four source labels

`source` records where the **case** came from. It says nothing about where the code came
from — every code in every case, regardless of label, is verified against the tariff by
`build_golden.py`.

**`verified-live`** (3 cases) — already produced by this system against the live API and
checked against the tariff by a human. The smallest and most expensive source, and the only
one whose *difficulty* is honest: the wording is what a person actually typed.

**`v1-prompt-example`** (5 cases) — the three products v1 named inside its own system
prompt: `iPhone 15`, `Geotex GTX-R3i`, `Ruger AR-556 MPR`. v1 named them and then never
measured what it did with them. Two of the three are bare model designations, which are not
product descriptions — they state no material, construction or measurement. So each of those
appears twice: once as the bare name, with the expectation the name can actually carry, and
once rewritten with the facts the tariff splits on, at full 10-digit precision.

**`tariff-derived`** (37 cases) — the bulk, and the strongest source for *correctness*.
A terminal leaf was chosen by walking the real JSON, and the Ukrainian description was then
written **from** its `full_path`. Read the honest limits below before trusting a number
built mostly on these.

**`domain-reasoning`** (10 cases) — the expectation comes from classification doctrine
rather than from reading a leaf: the vague and system-question behaviour cases, and the
adversarial cases built from failure modes v1 actually had. Their codes are still verified.

---

## Honest limits

Read this section before quoting an accuracy figure from this set.

**1. Tariff-derived descriptions are written FROM the answer, so they are easier than real
user input.** This is the big one. When the leaf says «віскі солодове» and the case input
says «Віскі шотландське солодове», the vocabulary already matches the tariff's own wording
and the retrieval problem is half solved before the agent starts. Real users write
«ноутбук», and the tariff writes «машини обчислювальні портативні» — `ILIKE '%ноутбук%'`
matches zero rows in this dataset. These 37 cases measure whether the **navigation** reaches
the right leaf. They do **not** measure whether the system can bridge consumer language to
tariff language, which is the harder half of the problem and the one embeddings are meant to
close in M4. Expect the score on this set to sit well above the score on real traffic, and
do not present it as production accuracy.

**2. 55 cases is a smoke test, not a statistic.** One case is 1.8 percentage points. A
three-case swing between runs is noise, not a regression. Use the per-tag and per-section
breakdowns to see *where* something moved before believing that anything moved at all.

**3. Residual «інші» leaves are excluded, and real traffic is full of them.** 2,473 of the
10,490 terminals are literally «інші». They are catch-alls that a human could not reach from
a product description either, so no case expects one — the verifier warns if you add one.
That makes the set systematically easier than the tariff is.

**4. Ambiguous leaves are avoided too.** 2,487 terminals (23.7%) share their `full_path`
with a sibling, because the real tariff separates them with header text this snapshot
dropped. The set uses none of them as an `expect_code`; `v1-geotex-described` is scored at
heading level precisely *because* both of its candidate leaves are ambiguous. The verifier
warns on any that slip in.

**5. Some of the ground truth is defensible, not certain.** Two cases carry `accept_codes`
for exactly this reason. `adv-rubber-boots` cannot be split between 6401 and 6402 without
knowing how the upper is attached, and `cons-hard-hat` cannot be split between 6506 10 10
and 6506 10 80 without knowing the material. Where a case says «defensible» rather than
«correct», the rationale says so.

**6. The brief for this set suggested `accept_codes: ["3919101100"]` on the film case.
That code does not exist in this dataset,** and the verifier rejects it. That is the whole
argument for `build_golden.py` in one line: a plausible-looking neighbouring code, written
in good faith by someone who knows the domain, was simply not real.

**7. Two known gaps in the tariff snapshot itself,** neither of them this set's doing:
heading **8712** (велосипеди) is absent, so a pedal bicycle has nowhere correct to go and no
case tests one; and sections **19 and 20** carry the same description, «Різні промислові
товари», in the source file, which is why they look like duplicates in the section
histogram above. Section 19 is in fact arms and ammunition (group 93).

**8. Nothing here measures cost, latency or tool-call count.** Those belong to the harness.

---

## Adding a case

1. **Find the leaf first, then write the description.** Never the other way round. Walk the
   tariff and read `full_path` end to end — never a bare `description`, because 2,473 leaves
   say only «інші».
2. **Pick a leaf that is neither residual nor ambiguous,** unless the case exists precisely
   to test that situation. `build_golden.py` will warn you either way.
3. **Write the `rationale` as an argument against the neighbours.** "Why this leaf and not
   the one next to it" is the test. If you cannot write that sentence, you do not have
   ground truth — use `expect_heading`, or expect an outcome and no code at all.
4. **Choose the granularity honestly.** If the 10-digit split turns on a fact your `input`
   does not state, either put the fact in the input or drop to `expect_heading`. Do not
   guess and then call the guess ground truth.
5. **Give it an id that will not change** and put it in the file whose theme it matches.
6. **Run the verifier.** It must print `PASS` and exit 0.

```bash
.venv/bin/python evals/build_golden.py
```

---

## Promoting a production misclassification

This is how the set is supposed to grow. A golden set assembled once and never touched
again decays into a fixture; the point is to feed real failures back in.

1. **Find the turn** in the records. You need the `request_id`, the user's exact text, the
   emitted code(s) and the outcome. `TurnLedger` has the evidence for the codes and
   `app/records/` has the turn.
2. **Establish the truth, by hand.** Open the tariff, walk it yourself, and decide what the
   code should have been. If you cannot defend one leaf over its neighbour, the honest
   outcome is `expect_heading` — or the conclusion that the *user's description* was
   underspecified and the correct behaviour was `clarification`, which makes it a
   behaviour case, not a code case.
3. **Copy the user's wording verbatim** into `input`. Do not tidy the typos, do not add the
   material the user omitted, do not translate consumer words into tariff words. The whole
   value of a promoted case is that it is the only kind of input in this set that was not
   written by someone who already knew the answer.
4. **Tag it `from-production`** alongside its domain tags, so the breakdown can separate the
   cases that came from real traffic from the ones written against the tariff. Set
   `source: verified-live` only if a human confirmed the correct code against the tariff;
   otherwise `domain-reasoning`.
5. **Write the rationale as the diagnosis,** not just the answer: say which neighbouring
   leaf the system chose and what in the description should have ruled it out.
6. **Run the verifier, then re-run the baseline.** A promoted misclassification is expected
   to fail on the first run. That failing case is the unit of work for the next prompt
   change — which is the thing v1 never had.

---

## Files

| file | cases | theme |
| --- | --- | --- |
| `golden/verified_live.yaml` | 3 | Turns already run against the live API and checked by a human. |
| `golden/v1_prompt_examples.yaml` | 5 | The three products v1 named in its own prompt, bare and described. |
| `golden/behaviour.yaml` | 8 | Vague input, questions about the system, and four threshold pairs. |
| `golden/adversarial.yaml` | 6 | Name-vs-material, non-terminal codes echoed by the user, two goods in one message. |
| `golden/food_and_agriculture.yaml` | 7 | Sections I-IV. |
| `golden/materials_and_chemicals.yaml` | 8 | Sections V-X and XIII. |
| `golden/machinery_and_transport.yaml` | 8 | Sections XVI-XVIII. |
| `golden/consumer_goods.yaml` | 10 | Sections VIII, XI, XII, XIV, XVIII, XX, XXI. |
| `build_golden.py` | — | The verifier. Also the loader the harness imports. |

## What the harness imports

`build_golden.py` is the single implementation of the schema — the harness should not parse
the YAML itself:

```python
from evals.build_golden import DEFAULT_TARIFF, GoldenCase, TariffIndex, load_cases, verify

cases = load_cases()                        # list[GoldenCase], filename order
index = TariffIndex.load(DEFAULT_TARIFF)    # offline tariff lookup, no DB
report = verify(cases, index)               # report.ok, report.failures, report.warnings
```

`evals/` is an implicit namespace package: the import works from the repo root, which is
already on `sys.path` for the test suite (`pythonpath = ["."]` in `pyproject.toml`).

`GoldenCase` is frozen and slotted, with `.id`, `.input`, `.expect_outcome`, `.expect_code`,
`.expect_heading`, `.accept_codes`, `.rationale`, `.source`, `.tags`, `.source_file`, and
`.all_codes` (expected first, then accepted). `load_cases()` raises `GoldenSetError` — which
carries `.failures`, every structural problem, not just the first — if any file is malformed.
