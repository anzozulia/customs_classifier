# Milestone: classification history

> "We need to implement some kind of history of classifications and lookups."

The SPA has shipped `/history` and `/history/:id` since the frontend milestone, calling three
endpoints that did not exist. `app/records/` was an empty directory. This milestone fills it
in, wires it into the turn, and mounts it on the app.

---

## What was built

### 1. The schema — `migrations/versions/0004_records.py`

`revision = "0004_records"`, `down_revision = "0003_tariff"`, and the single alembic head.
Three tables:

| table | what it holds |
|---|---|
| `classification` | one row per assistant turn: the input, the outcome, the run parameters (model, prompt version + digest, dataset digest), the token bill, the cost and the two latencies |
| `classification_code` | the codes that answered, `position`-ordered, with the tariff's own `description` and `full_path` denormalised onto the row |
| `classification_tool_call` | the turn's tool trace: name, JSONB arguments, result digest, duration, ok/error |

Two indexes on the parent: `classification_user_created_idx (user_id, created_at DESC, id DESC)`
— the keyset index the list endpoint reads a page from — and
`classification_input_text_trgm_idx`, a GIN `gin_trgm_ops` index over `input_text` that makes
`?q=` a search instead of a sequential scan. `pg_trgm` itself is created by migration 0003 and
is not touched here.

`id` is `cls_` + `secrets.token_urlsafe(18)`, not a sequence: it is the `/history/:id` path
segment, and a BIGSERIAL would make one user's register an enumerable neighbourhood of
everyone else's. `thread_id` is TEXT NULL with **no** foreign key — an eval or CLI run has no
chat thread, and deleting a conversation must not delete the record of what was classified in
it. Money is `NUMERIC(10,6)`, never a float.

### 2. The writer — `app/records/writer.py`, `app/records/pricing.py`

Three calls and one transaction:

```python
id = await begin_turn(ctx, input_text=…, thread_id=…, model=…, …)   # INSERT … outcome='pending'
… the model runs …
await finish_turn(id, outcome=agent_ctx.outcome, codes=…, …)        # or
await fail_turn(id, error_class=classify_error(exc), …)
```

**The row is INSERTed before the model runs.** That is the whole design. v1 wrote a record
only when a turn succeeded, so every crashed, cancelled or wall-clocked turn left nothing at
all behind and its 1,403 classifications produced zero usable input/output pairs. Here a turn
that dies between the two calls stays `pending` with a NULL `finished_at` — a row you can
query, not an absence you cannot.

Codes are resolved through `TurnLedger.evidence_for()` by `codes_from_ledger()`, so the stored
`description`/`full_path` are the database's text at answer time and never the model's; a code
with no evidence this turn is dropped with an ERROR rather than stored with invented text.
Tool calls use `position = ToolCallRecord.index` (the ledger's own numbering, which
`CodeEvidence.tool_call_index` points at). `cost_usd` comes from a versioned per-model price
table and is NULL for a model the table does not know — a plausible-looking wrong cost is
worse than a missing one. The configured default, `gpt-5.6-terra`, is priced.

Every public call is wrapped in `try/except Exception` + `logger.exception` with the
classification id. Not v1's fail-open: v1 had fifteen bare `except:` blocks that returned
empty state and left no trace, so an outage looked exactly like a user with no history.
`asyncio.CancelledError` is a `BaseException` in 3.13 and is deliberately not caught.

### 3. The API — `app/records/routes.py`

```
GET /api/history?q=&before=&limit=20   -> HistoryPage
GET /api/history/{id}                  -> HistoryDetail   (404, never 403)
GET /api/history/export.csv?q=         -> text/csv, streamed, UTF-8 BOM
```

Keyset pagination, never OFFSET: the cursor is base64url of `"<iso>|<id>"` and the predicate
is the row-value comparison `(c.created_at, c.id) < ($2::timestamptz, $3::text)`, which is the
exact shape of the composite index. `limit + 1` rows are fetched so `next_before` can learn
there is a next page without a COUNT, and it is null on the last page (the SPA hides
«Показати ще» on null). A page and its codes are one round trip: a CTE LEFT JOINed to
`classification_code` and regrouped in Python. `q` searches `input_text` (trigram, ILIKE) OR
the child table's `code`, so «плівка» is the description and `3919` is the code.

`export.csv` is declared **before** `/history/{id}`: Starlette matches in registration order
and `{id}` would otherwise swallow the literal segment. It streams a server-side cursor inside
a transaction rather than materialising the whole register in memory.

Every statement carries `user_id`, including the two child queries in the detail path, which
join back to `classification` and repeat the predicate although the id was just proved owned —
so "does this statement mention `user_id`" stays mechanically checkable.

### 4. The wiring (this pass) — `app/chat/server.py`, `app/main.py`

`respond()` now opens the record immediately before `Runner.run_streamed` and closes it in the
**same `finally` that already logged the turn summary**, from the same local variables:

```python
ttfb_ms = _ms_between(started, first_event_at)
duration_ms = _ms_since(started)
logger.info("turn done user=%s … ttfb_ms=%s latency_ms=%s", …, ttfb_ms, duration_ms)
if error_class is None:
    await finish_turn(classification_id, outcome=agent_ctx.outcome,
                      codes=codes_from_ledger(ledger, agent_ctx.emitted_codes), …)
else:
    await fail_turn(classification_id, error_class=error_class, …)
```

One reading of the clock, two sinks. `tests/test_chat_wiring.py::test_ttfb_and_duration_match_the_log_line`
parses the log line back and compares it to what the writer received, so a future edit that
recomputes either number fails the suite. **The `turn done` line is unchanged** — same format
string, same argument order — because `scripts/spike_m0.py` greps it for `turns=`, `in=` and
`out=`.

Three inputs the record needs that were not already in that block:

* **`input_text`** — `_message_text(input_user_message)`, falling back to `_last_user_text(input_items)`.
  The fallback is not defensive padding: answering a clarification produces no
  `UserMessageItem` at all (`input_user_message` is None on that path), so the only place that
  turn's input exists is the converted history.
* **`dataset_sha256`** — a new `ClassifierServer.dataset_sha256()`, cached like the catalogue
  and for the same reason. It swallows and warns, so a `TariffRepo` stand-in without the
  method (the M0 spike ships one) cannot take the stream down with an `AttributeError`.
* **`clarification_question`** — sniffed off the wire in the event loop. It genuinely exists
  nowhere else: `UktzedContext` carries the clarification *outcome* but not its text, and
  `ask_clarification`'s ledger entry records only the option count. Reading the
  `StructuredInputItem` as it streams past keeps the record-keeping out of the tool. (The
  alternative was a new field on `UktzedContext` plus a write in `tools_terminal.py` — two
  files outside this pass, for the same value.)

No record is opened for a locked/closed thread or for an empty model input: both return before
`begin_turn`, so neither can leave a `pending` row that nothing will ever close.

`app/main.py` includes `records_router` next to `auth_router` and `chat_router` — which is to
say **before** the `_SpaFiles` mount at `"/"`. Starlette matches in registration order, so a
router included after that mount is unreachable and every `/api/history` request would be
answered by the static handler's 404.

---

## The outcome vocabulary, and why it collapses where it does

There are three vocabularies, and the decision is *where* each one is translated.

| agent (`app.agent.Outcome`) | stored (`classification.outcome`) | API (`web/src/lib/history.ts`) |
|---|---|---|
| `result` | `classified` | `classified` |
| `clarification` | `clarification` | `clarification` |
| `conversation` | **`conversation`** | `classified` (collapsed) |
| `error` | `error` | `error` |
| — | `pending` (set by `begin_turn`) | `pending` |

**The column stores the lossless five-value set; the collapse to the contract's four happens at
the API boundary, in `_OUTCOME` in `routes.py`.** `conversation` — the agent answered in prose
without ever reaching a terminal tool, 29 of v1's 1,403 turns — is a real and distinct thing
that the record exists to count. Collapsing it in the writer would make "how often does the
agent chat instead of classifying?" unanswerable from the table, which is one of the few
questions this table is for. Collapsing it in the frontend would mean widening a type file that
is deliberately the contract.

Three consequences worth stating plainly:

* A chitchat turn («дякую») is a row, stored `conversation`, and the register renders it as
  «класифіковано» with no codes. That is the cost of the four-value badge, and it is cosmetic:
  the distinction survives in the column for anyone querying it.
* The agent can set `outcome = "error"` itself, without an exception — that is
  `finalize_on_terminal_tool` giving up after the repair budget is spent. Such a turn is closed
  by `finish_turn` with `outcome='error'` and `error_class = NULL`, which is correct: it failed,
  but no client library raised. `error_class` is only ever written by `fail_turn`, from the
  closed vocabulary in `app/chat/errors.py`.
* An outcome outside the agent's own `Literal` is logged at ERROR and recorded as `error`
  rather than tripping the CHECK constraint and costing the whole row.

`_OUTCOME`'s `.get(…, "classified")` fallback exists so that a value the column grows later
degrades to a valid badge rather than rendering `OUTCOME_LABEL[undefined]` in the SPA.

---

## What is verified

All commands run from the repo root, against `.venv`, with no Postgres, no docker and no calls
to the live API.

```
$ .venv/bin/python -m ruff check app tests scripts migrations
All checks passed!

$ .venv/bin/python -m pytest -q
116 passed, 41 warnings in 1.54s

$ .venv/bin/python -c "import app.main"
(no output — imports cleanly)

$ cd web && npm run build
> tsc --noEmit && vite build
✓ 53 modules transformed.
dist/assets/index-Cxkv9Mz3.js   285.72 kB │ gzip: 90.75 kB
✓ built in 862ms
```

61 tests existed before this milestone; **116 pass now**. The 55 new ones are
`tests/test_records_writer.py` (20, the writer against a SQLite mirror of 0004),
`tests/test_history_api.py` (19, the routes both against that mirror and through a
`TestClient`), and `tests/test_chat_wiring.py` (16, this pass).

Also verified directly:

* **The migration renders the mandated DDL.** `alembic upgrade 0003_tariff:0004_records --sql`
  emits exactly the specified columns, the two indexes (including `USING gin (input_text
  gin_trgm_ops)`), both `UNIQUE (classification_id, position)` constraints and the `outcome`
  CHECK. `alembic heads` reports `0004_records (head)` — one head.
* **The router is mounted and reachable.** `app.main.app.openapi()["paths"]` contains all three
  history paths with `/api/history/export.csv` ahead of `/api/history/{entry_id}`, and an
  anonymous `TestClient` GET of each returns **401 JSON** — i.e. the router answers, not the
  `web/dist` mount (which is registered, and last).
* **The record is opened before the model runs**, with the turn's real rendered-prompt digest,
  `PROMPT_VERSION`, `settings.model` and the tariff's `dataset_sha256`.
* **Codes reach the writer resolved through the real ledger.** `emitted_codes` carries bare
  code strings; what `finish_turn` receives is `RecordedCode(code, description, full_path,
  is_primary)` built from `CodeEvidence`. A code with no evidence this turn arrives as an empty
  list.
* **A failed turn is closed as an error** with its token bill and tool trace intact, for both
  a generic exception (`internal`) and `MaxTurnsExceeded` (`max_turns_exceeded`).
* **A persistence failure neither breaks the stream nor goes quiet.** With no pool installed —
  the M0 spike's exact situation — the turn streams to the end and both halves log at ERROR
  with the same `cls_…` id.
* **The clarification question survives the sniff**: it reaches the writer *and* the
  `StructuredInputItem` still reaches the client.

---

## What still needs a live run or a browser

Nothing below is known-broken. It is the list of things this pass could not prove.

1. **The migration has never been applied.** No docker, so `make migrate` against Postgres 17
   is unrun. Rendering the SQL offline is not the same as `pg_trgm` being present and the GIN
   index actually building.
2. **No SQL in this milestone has touched Postgres.** Both test suites run the real statements
   against SQLite doubles with `$n` → `?` translation. Postgres-only behaviour that only a live
   run can confirm: the row-value keyset comparison and its use of the composite index; `ILIKE`
   case-folding for Cyrillic (SQLite's LIKE folds ASCII only, so the tests assert search with
   the stored casing); `$4::jsonb` on insert and JSONB coming back as `str`; `NUMERIC(10,6)` →
   `Decimal`; and `conn.cursor()` streaming the CSV export off a server-side cursor.
3. **A real end-to-end turn.** The wiring tests script `stream_agent_response`; nothing yet has
   run a live classification and then read it back through `/api/history/{id}`. That is the
   check that would catch a column-name disagreement between the writer's INSERT and the
   routes' SELECT, which so far has only been verified by eye and by two independent SQLite
   mirrors of the same migration.
4. **The browser.** `/history` and `/history/:id` have never rendered real rows: pagination
   («Показати ще» appearing and then disappearing on the last page), the search box, the code
   badges, «альтернатива», and the CSV download opening in Excel with Ukrainian intact are all
   unexercised. The SPA builds and typechecks against this JSON, which is not the same thing.
5. **`scripts/spike_m0.py` has not been re-run** (it needs the live API). It will now print two
   ERROR tracebacks per turn — `record begin_turn failed: PoolNotInitialisedError` and
   `no pending record cls_… to close` — because the spike runs with a `MemoryStore` and no
   pool. That is the designed fail-soft behaviour and the spike's own assertions read the
   `turn done` line, which is unchanged; `tests/test_chat_wiring.py` reproduces exactly this
   situation and asserts the stream survives it.
6. **Cancelled turns stay `pending` forever.** When the client disconnects, the `await` in the
   `finally` raises `CancelledError` and the row is never closed. That is deliberate — it is
   the state the schema has for it — but there is no reaper and no
   `outcome='pending' AND finished_at IS NULL AND created_at < now() - interval '1 hour'`
   sweep. The query is trivial; the decision to run it is not made here.
7. **`user_id` must exist.** `classification.user_id` is a real FK to `app_user(id)`, so any
   harness that invents a user id gets a logged FK violation and no row. Real traffic always
   has one (the router and the ChatKit endpoint are both behind `current_user`).
8. **`PRICING_VERSION` is a hand-maintained table.** `cost_usd` is NULL for any model outside
   it, including a dated snapshot of a model that is in it.
