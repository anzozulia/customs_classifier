# Integration report — uktzed-v2

Five chunks were written in parallel against fixed contracts and merged without ever being
imported together. This pass made the repository importable, reconciled the seams between the
chunks, and verified as much as can be verified without a live Postgres or an OpenAI key.

**Environment used for verification.** The machine had network access, so a throwaway venv was
built with the real pinned SDKs — `openai-chatkit==1.6.5` (from the git tag), `openai-agents==0.22.2`,
`pydantic==2.12.4`, `fastapi==0.120.4`, `asyncpg`, `alembic`, `pytest`, `ruff==0.14.5` — plus
node 24 / npm 11 for the frontend. So the claims below about the SDK surface are from *running*
the code against the pinned packages, not from reading them.

The venv lives at
`/private/tmp/claude-501/-Users-anzozulia-Desktop-edu-classifier-v2/dcd012c0-f1c3-4f1d-acda-57bafb1f6ccc/scratchpad/venv`
and is session-scoped; recreate it with the commands at the bottom.

---

## 1. What was broken, and what was changed

### 1.1 BLOCKER — circular import: `app.main` did not import at all

`app/context.py:12` does `from app.agent.provenance import TurnLedger`, which runs
`app/agent/__init__.py`, which did `from app.context import RequestContext` at module scope.
Importing `app.context` first therefore raised:

```
ImportError: cannot import name 'RequestContext' from partially initialized module 'app.context'
             (most likely due to a circular import)
```

`app.chat.store`, `app.chat.routes` and `app.main` all reach `app.context` first, so **the ASGI
entry point `app.main:app` could not be imported** — `uvicorn app.main:app` and every container
start would have died immediately. Importing `app.agent` first happened to work, which is why it
looked intermittent. (M2 reported this; it was reproduced here before fixing.)

`RequestContext` is needed at class-creation time (`AgentContext[RequestContext]` is a runtime
subscript), so `if TYPE_CHECKING:` cannot fix it. The import had to leave the package `__init__`.

**Fix:** `UktzedContext` and `Ctx` moved out of `app/agent/__init__.py` into a new
**`app/agent/context.py`** — which is the location the architecture's §5.4 already specifies.
`app/agent/__init__.py` now imports nothing but `pydantic` and `app.tariff`, and keeps `UAModel`,
`PathStep` and `Outcome`. Both modules carry a docstring explaining the cycle so nobody moves it
back.

Call sites updated to `from app.agent.context import Ctx, UktzedContext`:
`app/agent/agent.py`, `app/agent/tools_nav.py`, `app/agent/tools_terminal.py`,
`app/chat/server.py`, `tests/test_gate.py`.

> **Contract change.** The M0 chunk documented `app.agent.UktzedContext` / `app.agent.Ctx`.
> Those names now live at `app.agent.context`. No lazy re-export shim was added — a shim would
> hide the layering rule that this file exists to enforce.

### 1.2 The SPA could never be logged into — `GET /api/config` had no `user`

`web/src/components/AppShell.tsx:31` is `if (!config.user) redirect('/login')`. The M0 chunk's
`/api/config` returned only `{"domainKey", "locale"}`, so `config.user` was always `undefined`
→ `null`, and **every authenticated page bounced straight back to `/login`, forever, with a 200
on every request.** The frontend contract (`web/src/lib/config.ts`, `web/README.md`) asks for
`{domain_key, locale, chatkit_url, user}`.

**Fix** (`app/chat/routes.py`): the route now returns `domain_key`, `locale`, `chatkit_url` and
`user`. It stays **public** — it calls `current_user(request)` and swallows only the
`HTTPException`, so the session_epoch revocation check is reused verbatim while an anonymous
caller still gets 200 (`/login` must render without triggering the 401 interceptor).
Both `domain_key` and `domainKey` are emitted: `config.ts` accepts either, the two chunks
documented different spellings, and one duplicated public string is cheaper than a deploy-day
mismatch whose symptom is a self-deleting iframe.

### 1.3 Every login attempt would have been a 422 — body encoding mismatch

`web/src/pages/LoginPage.tsx` posts a `URLSearchParams`, i.e.
`application/x-www-form-urlencoded`. `app/auth/routes.py` declared `body: LoginRequest`, i.e. a
JSON body. Two contracts in the brief disagreed; the architecture doc breaks the tie —
line 2637 is `async def login(request: Request, username: str = Form(...), ...)`, and
`python-multipart` is pinned for exactly this.

**Fix:** `async def login(request: Request, body: Annotated[LoginRequest, Form()])`. The
exported `LoginRequest` model is kept (FastAPI ≥0.113 supports a pydantic model as a form), so
the `min_length`/`max_length` constraints stay declared once. A JSON body now 422s.

### 1.4 `python -m app.cli ingest` did not exist

`make ingest`, the Dockerfile comment and `README.md` all call
`python -m app.cli ingest data/uktzed_hierarchical.json`. `app/cli.py` had only the four user
commands; the ingest logic was reachable only through `scripts/ingest.py`, and `scripts/` is in
`.dockerignore` and is not `COPY`ed into the image — so `make ingest` would have failed inside
the container with no way to load the tariff.

**Fix:** added an `ingest` command to `app/cli.py` that calls the same
`app.tariff.ingest.ingest_file`, borrowing a connection out of the CLI's short-lived pool
(`copy_records_to_table` is connection-level, not pool-level).

### 1.5 `app/main.py` served no static files

The deployment chunk's contract says the built SPA lives at `/srv/web/dist` and "app/main.py
must mount this path"; `web/README.md` says the server must fall back to `index.html` for
unknown paths or a reload on `/history/<id>` 404s. Neither existed.

**Fix:** `app/main.py` gained a `_SpaFiles(StaticFiles)` subclass (SPA fallback: a 404 on a
request that accepts `text/html` serves `index.html`; a missing `/assets/*.js` still 404s) and
mounts it at `/` — **registered as the very last statement in the file**, because Starlette
matches routes in registration order and a `/` mount declared any earlier swallows `/healthz`
and `/readyz`. The mount is skipped with a warning when `web/dist` is absent, so a fresh
checkout still boots the API.

### 1.6 `make spike` pointed at a file that does not exist

The target was `uvicorn scripts.spike:app --reload --port 8001`. The M0 chunk shipped
`scripts/spike_m0.py`, which is a standalone PASS/FAIL harness with a `run_spike()` coroutine
and no module-level `app`.

**Fix:** `make spike` → `OPENAI_AGENTS_DISABLE_TRACING=1 python scripts/spike_m0.py`.
Also moved `scripts/spike_m0.py`'s `#!/usr/bin/env python3` to line 1 (the `# ruff: noqa` block
had been placed above it, which demotes a shebang to an ordinary comment) and `chmod +x`'d both
scripts. Ruff still honours the file-level `noqa` after a shebang — verified.

### 1.7 `web/package-lock.json` was missing

`Dockerfile:18` runs `npm ci`, which fails by design without a lock file. The deployment chunk
flagged it as something it could not produce.

**Fix:** generated by `npm install` (192 packages). It is committed-ready and the exact pins in
`web/package.json` all resolved.

### 1.8 New regression test for the seams — `tests/test_http_contract.py`

11 tests, no database and no network (the asyncpg pool is a stub answering the four queries the
auth path and `/readyz` actually issue). It pins §1.2, §1.3 and §1.5 — the three failures that
were invisible to every existing test because each chunk only tested its own half.

---

## 2. Verified working (real output)

### 2.1 Every Python module imports, in a fresh process each

```
app.settings  app.context  app.db  app.agent.provenance  app.agent  app.agent.context
app.agent.schemas  app.agent.prompts  app.agent.tools_nav  app.agent.tools_terminal
app.agent.agent  app.tariff  app.tariff.ingest  app.tariff.repo  app.tariff.validate
app.tariff.catalogue  app.auth.passwords  app.auth.deps  app.auth.routes  app.chat.store
app.chat.converter  app.chat.errors  app.chat.server  app.chat.routes  app.main  app.cli
```

All 26 → OK. `python -m compileall -q app tests scripts migrations` → clean.

With **no** third-party packages at all (system python3, stdlib only):
`app.tariff`, `app.tariff.ingest`, `app.agent.provenance` import. `app.settings` does not — it
needs `pydantic_settings`, which is by design.

### 2.2 Test suite — 61 passed

```
$ python -m pytest -q
61 passed, 41 warnings in 1.30s
```

`pytest tests/` also passes (M2 suspected it would not; it does — verified both ways).
Per file: `test_gate.py` 20, `test_http_contract.py` 11, `test_ingest_invariants.py` 19,
`test_store_isolation.py` 11. Run in reverse order too, to rule out ordering coupling: 61 passed.

### 2.3 Tariff data layer, against the real 2,517,169-byte JSON

`python3 tests/test_ingest_invariants.py` (stdlib only, no DB, no SDKs) — 19/19 ok:

```
sha256                5a113fc09ac05028afbd6ed1884a95133b65e7827e61a87d74d5ffb11a38dee3
nodes                 14,187
  sections            21
  groups              97
  categories          957
  prefix-tree codes   13,112
terminals             10,490
ambiguous full_path   2,487
dead ends             ['77']
max depth             5
empty full_path       0
```

`python3 scripts/ingest.py --check-only` → `invariants OK`, same numbers.
The sha256 matches the pin in the brief.

### 2.4 The agent actually builds, with 9 strict tool schemas

```
prompt chars 7903
agent uktzed-classifier  model gpt-5.6-terra  tools 9
  list_groups_in_section     strict=True  args=['section_code']
  list_categories_in_group   strict=True  args=['group_code']
  open_category              strict=True  args=['category_code']
  expand                     strict=True  args=['prefix']
  search_candidates          strict=True  args=['query', 'limit']
  resolve_code               strict=True  args=['code']
  web_search                 hosted WebSearchTool
  emit_classification        strict=True  args=['codes','confidence','rationale','path',
                                                'alternatives','product_summary','evidence']
  ask_clarification          strict=True  args=['question','options','why']
model_settings: Reasoning(effort='low', summary='auto')  verbosity=low  timeout=90.0
                include_usage=True  store=False  parallel_tool_calls=True
tool_use_behavior callable: True
```

Every icon the code uses is in the SDK's `IconName` literal: `compass`, `book-open`, `search`,
`cube`, `document`, `check-circle`, `circle-question`. `CustomTask`, `CustomSummary`,
`ProgressUpdateEvent`, `StructuredInputItem(status="pending")`, `AssistantMessageItem`,
`ErrorEvent`, `CustomStreamError(message, allow_retry=)` all construct.
`AgentContext` really does expose `add_workflow_task`, `end_workflow`, `stream`, `generate_id`.
`NonStreamingResult.json` really is the bytes attribute (`self.json = result`), so
`app/chat/routes.py` is right.

### 2.5 Navigation tools drive end to end against a `TariffRepo`-shaped object

Driven through `FunctionTool.__wrapped__` with the spike's in-memory tariff and `MemoryStore`:
`list_groups_in_section`, `list_categories_in_group`, `open_category`, `expand`, `resolve_code`,
`search_candidates` all return their envelopes; the breadcrumb ends at the opened node; a bad
section code and an unknown category both come back as friendly errors with the breadcrumb kept;
the ledger recorded 7 codes; workflow `CustomTask`s were pushed; `emit_classification` accepted
the walked code and rejected an unwalked one with `provenance_violation`.

### 2.6 HTTP seams (FastAPI TestClient, stub pool)

All green: `/healthz`; `/readyz` (db + dataset + model); `/api/config` public with `user: null`;
`/api/config` reporting the user after login; form-encoded `/api/login` → 200; wrong password →
401; missing `Origin` → 403; `/chatkit` CSRF-checked before `process()`; `/api/logout` → 204;
`/api/me` 401 → 200 → 401 across the session lifecycle; SPA served at `/` without shadowing
`/healthz` or `/api/*`; `/history/abc` falls back to `index.html`; `/assets/nope.js` still 404s.

### 2.7 Frontend

```
$ npx tsc --noEmit          # zero errors
$ npm run build
✓ 53 modules transformed.
dist/index.html                   1.79 kB
dist/assets/index-Br6PG_j8.css   17.45 kB
dist/assets/index-Cxkv9Mz3.js   285.72 kB │ gzip: 90.75 kB
✓ built in 710ms
```

`web/dist/` is exactly the path `app/main.py` mounts and the Dockerfile copies.

### 2.8 Alembic

```
$ alembic history
0002_chatkit_store -> 0003_tariff (head)
0001_users -> 0002_chatkit_store
<base> -> 0001_users
$ alembic heads
0003_tariff (head)
```

Single head, chain intact. (Parsed offline; no DDL was executed.)

### 2.9 One open question from the deployment chunk is now answered

`uvicorn 0.38.0 --help` **does** list `--timeout-worker-healthcheck`. The
`docker-compose.yml` comment offering to delete that line can come out.

---

## 3. Must fix — in files this pass was told not to edit

### 3.1 `pyproject.toml` pins a version of alembic that does not exist

```
ERROR: Could not find a version that satisfies the requirement alembic==1.16.6
       (from versions: … 1.16.3, 1.16.4, 1.16.5, 1.17.0, …)
```

There is no 1.16.6 on PyPI. **This breaks `pip install` and the Docker `pybuild` stage**, which
reads the dependency list straight out of `pyproject.toml` with `tomllib`. The verification venv
used `alembic==1.16.5`, which installs and whose `alembic history` works against these
migrations.

```diff
-  "alembic==1.16.6",
+  "alembic==1.16.5",
```

### 3.2 `app/agent/provenance.py:63` fails `ruff check` — so `make lint` fails

```
E501 Line too long (108 > 100)
  --> app/agent/provenance.py:63:101
```

It is the only `ruff check` error in the whole repository. One line:

```python
    def end_call(
        self, rec: ToolCallRecord, *, digest: str | None = None, error: str | None = None
    ) -> None:
```

### 3.3 `pyproject.toml` — two quality-of-life additions every chunk asked for

Every chunk that writes Ukrainian ends up with a per-file `# ruff: noqa: RUF001, RUF002`
header, because ruff's defaults flag Cyrillic і/а/у/о and the en dash as ASCII confusables.
One project-wide setting replaces eight file headers:

```toml
[tool.ruff.lint]
select = ["E", "F", "W", "B", "I", "UP", "SIM", "RUF"]
allowed-confusables = ["і", "а", "у", "о", "б", "е", "с", "р", "х", "–", "«", "»"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["."]          # so a bare `pytest tests/` cannot depend on rootdir luck
```

Also consider moving `psycopg[binary]>=3.2,<4` into `dependencies` — the Dockerfile installs it
separately today because alembic needs a synchronous DBAPI and the only driver in `pyproject` is
asyncpg, and anyone running `alembic upgrade head` outside the container hits that too.

### 3.4 `.env.example` is missing two variables `docker-compose.yml` reads

`UKTZED_DOMAIN` (compose default `localhost`) and `ACME_EMAIL` (default `ops@example.com`), both
consumed only by Caddy. They work as-is on a laptop; a real deploy needs them set.

---

## 4. Known gaps — not defects, just not built yet

* **`/api/history`, `/api/history/{id}`, `/api/history/export.csv` do not exist.** The SPA ships
  `/history` and `/history/:id` pages that call them, so those two routes will show their error
  state. `app/records/` is an empty directory; this is the records/history milestone, and no
  chunk in this batch owned it. Everything else in the SPA (login, chat, theme) is wired to
  endpoints that exist.
* `app/widgets/` and `evals/golden/` are empty directories — later milestones.
* `app/agent/prompts/__init__.py:28` names `tests/test_prompt_version.py`, which does not exist.
  One assertion (`TEMPLATE_SHA256` matches a pinned constant) would make the docstring true.
* `ruff format --check .` reports 11 files. They were left alone on purpose: the formatting is
  cosmetic, the reflows would touch deliberately hand-laid-out Ukrainian string tables, and
  `make lint` cannot pass until §3.2 is fixed anyway. `make fmt` handles all of them in one go.
  Nothing in this pass added new format drift (checked file by file).

---

## 5. UNVERIFIABLE here — needs Postgres, a browser, or an API key

Nothing below was tested. Do not read §2 as covering it.

1. **All SQL.** No Postgres instance was available. `migrations/versions/0001-0003` were never
   executed; `TariffRepo`'s nine queries, `PgStore`'s 14 methods against real asyncpg, the
   `word_similarity()` search, the partial unique index on `tariff_dataset.is_active`, the
   `citext` and `pg_trgm` extensions, and the COPY-then-UPDATE ingest were all verified only as
   Python. `tests/test_store_isolation.py` executes the store's real SQL against SQLite with a
   `$n` → `?` translation, which proves the *predicates* and not the Postgres dialect.
2. **Any call to OpenAI.** No `OPENAI_API_KEY`. The model was never invoked, so
   `stream_agent_response` over a real tool-calling stream, the reasoning-summary →
   `ThoughtTask` path, `ModelSettings` acceptance by `gpt-5.6-terra`, the repair loop firing for
   real, and the hosted `WebSearchTool` are all unproven. `scripts/spike_m0.py` is the harness
   for exactly this and it aborts cleanly without a key — run it first.
3. **Anything that needs a browser.** ChatKit domain-key verification, the iframe mounting at
   all, `ProgressUpdateEvent` rendering, the Thinking panel, the live drill-down, the SSE
   stream surviving Caddy. The frontend was type-checked and built, never loaded.
4. **Docker and Caddy.** No daemon. The Dockerfile, `docker-compose.yml` and `Caddyfile` were
   read and cross-checked against the code (paths, the `app.main:app` entry point, the
   `python -m app.cli ingest` contract, the uvicorn flag in §2.9) but never built or started.
   The Caddyfile has still never been parsed by a caddy binary.
5. **`ingest_file` against a real database.** Its parse-and-assert half is proven against the
   real JSON (§2.3); the COPY, the parent-id wiring UPDATE, and the idempotency-per-sha256 path
   are not.
6. **Multi-user isolation in production shape.** Proven by mutation testing against SQLite, not
   against Postgres with two real sessions.

---

## 6. Exact next commands

```bash
cd /Users/anzozulia/Desktop/edu/classifier_v2/uktzed-v2

# 0. Fix the two blocking edits in the protected files first (§3.1, §3.2).
#    alembic==1.16.6 -> 1.16.5 in pyproject.toml, and wrap provenance.py:63.

# 1. A real venv (pip install FAILS until §3.1 is fixed).
python3 -m venv .venv && . .venv/bin/activate
pip install -e . 2>/dev/null || pip install \
  "openai-chatkit @ git+https://github.com/openai/chatkit-python.git@v1.6.5" \
  openai-agents==0.22.2 fastapi==0.120.4 "uvicorn[standard]==0.38.0" asyncpg==0.30.0 \
  alembic==1.16.5 sqlalchemy==2.0.44 pydantic==2.12.4 pydantic-settings==2.12.0 \
  itsdangerous==2.2.0 argon2-cffi==25.1.0 typer==0.20.0 python-multipart==0.0.20 httpx==0.28.1 \
  pytest==8.4.2 pytest-asyncio==1.3.0 ruff==0.14.5 "psycopg[binary]"

# 2. Re-run everything this report claims.
python -m compileall -q app tests scripts migrations
python -m pytest -q                      # expect: 61 passed
python3 tests/test_ingest_invariants.py  # expect: 19 ok, 14,187 nodes / 10,490 terminals
ruff check .                             # expect: clean once §3.2 is done
npm --prefix web ci && npm --prefix web run build

# 3. Database. (docker compose up -d db, or any local Postgres.)
cp .env.example .env     # set SESSION_SECRET, OPENAI_API_KEY, CHATKIT_DOMAIN_KEY
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/uktzed
alembic upgrade head
python -m app.cli ingest data/uktzed_hierarchical.json   # expect 14,187 / 10,490
python -m app.cli create-user anton                      # prints a password once

# 4. The first thing that has never run: the live-API spike.
OPENAI_API_KEY=sk-... OPENAI_AGENTS_DISABLE_TRACING=1 make spike

# 5. Then the app, and a browser.
uvicorn app.main:app --reload --port 8000
curl -s localhost:8000/readyz | python3 -m json.tool
curl -s localhost:8000/api/config | python3 -m json.tool   # user: null before login
#   open http://localhost:8000/login  — needs CHATKIT_DOMAIN_KEY registered for the hostname

# 6. Whole stack.
make up && make logs
```

### Files changed by this pass

```
NEW   app/agent/context.py            UktzedContext + Ctx (breaks the import cycle)
NEW   tests/test_http_contract.py     11 seam tests, no DB, no network
NEW   web/package-lock.json           generated; `npm ci` needs it
EDIT  app/agent/__init__.py           now imports only pydantic + app.tariff
EDIT  app/agent/agent.py              import moved
EDIT  app/agent/tools_nav.py          import moved
EDIT  app/agent/tools_terminal.py     import moved
EDIT  app/chat/server.py              import moved
EDIT  tests/test_gate.py              import moved
EDIT  app/chat/routes.py              /api/config: + user, chatkit_url, domain_key
EDIT  app/auth/routes.py              /api/login: form-encoded
EDIT  app/cli.py                      + `ingest` command
EDIT  app/main.py                     + SPA mount with fallback, registered last
EDIT  Makefile                        `spike` target points at the file that exists
EDIT  scripts/spike_m0.py             shebang to line 1; +x
```

None of the six pre-written files (`pyproject.toml`, `.env.example`, `app/settings.py`,
`app/context.py`, `app/agent/provenance.py`, `data/uktzed_hierarchical.json`) was modified.
Nothing was committed.

`web/node_modules/` and `web/dist/` are now present as build artefacts; both are already in
`.gitignore`.
