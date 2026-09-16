"""The evaluation harness — the deepest flaw inherited from v1, fixed.

v1 had no evaluation at all. `test_agents.py` called eight test functions that were never
defined, so it crashed on import in every single commit; 1,403 production classifications
produced zero measured accuracy, and that is exactly why one prompt line changed in ten
commits. Nobody could tell whether a change helped.

Four modules, one job each:

* `evals.harness` — run ONE case through `ClassifierServer.respond()`: the real agent, the
  real prompt, the real tools over a real `TariffRepo`, and a structured `Observation` back.
* `evals.scoring` — the metrics, as pure functions over `(Case, Observation)`, plus the report
  and the regression gate. **Read this file before trusting a number.**
* `evals.run` — the CLI: `python -m evals.run --tag plastics --model gpt-5.6-luna`.
* `evals.build_golden` — the golden set's own loader and the offline ground-truth verifier.
  It owns the case schema; `evals.scoring.load_cases` delegates a YAML directory to it rather
  than parsing the files a second time, so there is exactly one definition of a valid case.

Golden cases live in `evals/golden/` as `.yaml` — one file per theme, each a bare list of
cases. `evals.build_golden` documents and enforces the schema; `evals.scoring.Case` documents
how each field is scored. `evals/baseline.json` is the recorded scorecard the gate compares
against. `make eval-verify` checks every case against the tariff with no API key and no
database, which is why it is the one eval target that belongs in CI.

This package `__init__` imports nothing on purpose. `evals.scoring` must stay importable with
no database, no API key and no Agents SDK — `tests/test_eval_scoring.py` runs entirely offline
against it — and re-exporting the harness from here would drag the whole agent layer in.
"""

from __future__ import annotations

__all__: list[str] = []
