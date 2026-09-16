# UKTZED Classifier v2

A web chat that classifies a product description into a Ukrainian customs tariff
(УКТЗЕД) code. You describe the goods in Ukrainian; the agent walks the tariff tree
— section → group → category → subheading — and answers with a code, the full
ancestor path that justifies it, and the alternatives it rejected.

v2 of a Telegram bot. The classifier logic survived; almost nothing else did. The
three things that changed, because they were measured and found broken:

- **It streams.** v1 showed a static `🔍 Обробляю ваш запит...` for a cold p50 of
  48 seconds. Only 7.5% of its turns finished under 10s.
- **It records.** v1 logged nothing: no queries, no answers, no codes, no tool calls,
  no token counts. Every classification now writes a row before the model runs and
  updates it after, so failed turns are visible too.
- **It cannot invent a code.** A code may be emitted only if *this turn's* tool
  results contained it (`app/agent/provenance.py`). v1's measured rate of carrying a
  stale code over from an earlier turn was 36%.

Stack: Python 3.13, FastAPI, OpenAI Agents SDK (`openai-agents`), ChatKit
(`openai-chatkit`, self-hosted server mode), Postgres 17, React + Vite, Caddy.

---

## ⚠️ This is not a self-contained application. Read this before you deploy.

The agent is yours and runs on your server. **The chat UI is not.** It is loaded from
OpenAI's CDN and rendered inside a cross-origin iframe, and that has consequences you
must plan around rather than discover on launch day:

1. **`chatkit.js` is fetched from `https://cdn.platform.openai.com` at page load.**
   It cannot be vendored: the bundle reads `document.currentScript.src` at module
   evaluation to derive its own iframe URL, so re-hosting it breaks the frame. It is
   unversioned (`max-age=300`) and auto-updates. If OpenAI's CDN is down, your app has
   no chat.

2. **Production verifies your domain against an org allowlist, and it FAILS CLOSED.**
   The loader POSTs to `https://api.openai.com/v1/chatkit/domain_keys/verify`. If the
   answer is negative — or never arrives — it calls `emit("unmount")` and **removes the
   chat element from the page**. Not an error toast. Not a degraded mode. The chat box
   is simply gone, and nothing in your server logs will mention it.

   - **Register the production hostname days ahead**, at
     `platform.openai.com/settings/organization/security/domain-allowlist`.
     Propagation was measured at minutes to about 30 minutes by two independent
     reporters. One `domain_pk_…` key covers up to 20 domains, so prod and staging can
     share one.
   - Put the key in `CHATKIT_DOMAIN_KEY`. It is **public** — it is served to the
     browser at runtime by `GET /api/config`, not baked into the JS bundle, which is
     what removes the "rebuild the frontend to change the domain" step.

3. **Verification is *skipped* on localhost, `127.x`, `0.0.0.0`, `[::1]`, `*.local`,
   any non-`https` origin and any non-443 port.** So local development always works,
   a staging box on `http://1.2.3.4:8000` always works, and **the first real HTTPS
   deploy is the first time the check ever runs**. Test the key on a real HTTPS
   hostname before launch day, with the DevTools console open.

4. **Every end user's browser must be able to reach `https://api.openai.com`.** The
   check fails closed on network errors too (10s abort, one retry). A corporate proxy
   that blocks OpenAI breaks the app for that user even though your server is fine.
   There is no workaround in the self-hosted path.

5. **You cannot style the chat.** The transcript, composer, header and history panel
   live in a cross-origin iframe. Tailwind reaches the page shell and nothing else;
   inside, you get ChatKit's `theme` object (`colorScheme`, `radius`, `density`,
   `typography`, `color`) and that is all.

The honest framing of this project is **"self-hosted agent, hosted UI"**. That trade
was made deliberately — it buys a production-grade streaming chat UI for zero
front-end work — but it is a real dependency on a third party in the render path.

---

## Quickstart

### 1. Configure

```bash
cp .env.example .env
chmod 600 .env
```

Fill in, at minimum:

| Variable | How to get it |
|---|---|
| `OPENAI_API_KEY` | platform.openai.com |
| `SESSION_SECRET` | `openssl rand -base64 48` |
| `POSTGRES_PASSWORD` | `openssl rand -base64 32` — and mirror it into `DATABASE_URL` |
| `CHATKIT_DOMAIN_KEY` | the domain allowlist (above). `domain_pk_localhost_dev` works locally |

Two more variables are read by `docker-compose.yml` for the proxy only, and are not in
`.env.example` because nothing else uses them. Add them to `.env` before a real deploy:

```dotenv
UKTZED_DOMAIN=uktzed.example.com     # defaults to `localhost` (Caddy's internal CA)
ACME_EMAIL=ops@example.com           # Let's Encrypt contact
```

### 2. Run

```bash
make up          # builds the image, starts db, runs migrations to completion, starts app + caddy
make ingest      # loads the tariff: expect 14187 nodes / 10490 terminals
make user USERNAME=anton
make logs
```

`make up` already runs the migrations — `migrate` is a one-shot service that `app`
waits on with `service_completed_successfully`. `make migrate` is for afterwards, when
you have added a revision.

Then open `https://localhost` (Caddy issues a certificate from its own internal CA, so
your browser will warn once) or `https://$UKTZED_DOMAIN`.

**There is no registration page anywhere in the app.** The absence of the route *is*
the "no self-registration" requirement. `make user` is the only way in.

### 3. Develop

`pyproject.toml` declares no `[build-system]` on purpose — this is an application, not a
library, and nothing imports it as a package. `uv` understands that shape directly:

```bash
uv sync                    # installs dependencies + the dev group; does not try to build the project
```

With plain pip, install the pinned dependencies without installing the project:

```bash
python3.13 -m venv .venv && . .venv/bin/activate
python -c "import tomllib; d = tomllib.load(open('pyproject.toml','rb')); \
  print('\n'.join(d['project']['dependencies'] + d['dependency-groups']['dev']))" > /tmp/req.txt
pip install -r /tmp/req.txt
```

Then:

```bash
pip install "psycopg[binary]"     # only to run alembic outside the container; see migrations/env.py
cd web && npm install && cd ..    # commit web/package-lock.json — `npm ci` in the image requires it

make dev                          # uvicorn --reload on :8000 + the Vite dev server, ctrl-C stops both
make test
make lint
```

For host-side development `DATABASE_URL` must point at a reachable Postgres: the
compose `db` service publishes **no** port. Either run a local Postgres, or bind the
container's port to loopback temporarily (`ports: ["127.0.0.1:5432:5432"]`) and remove
it when you are done. v1 published 5432 on `0.0.0.0` with the default password; that is
the one mistake this compose file is most careful not to repeat.

### 4. Prove the risky part first

```bash
make spike       # M0: ChatKit + Agents SDK against the real API, on :8001
```

Nothing in the ChatKit/Agents integration has ever been run against a live model. M0
exists to invalidate the design cheaply if it is going to be invalidated at all: a
tool-calling stream that completes, a rejected terminal tool that still yields another
model turn, a widget that renders.

---

## Make targets

| Target | What it does |
|---|---|
| `make help` | list all targets |
| `make dev` | API with reload + Vite dev server; ctrl-C stops both |
| `make up` | build and start the whole stack |
| `make down` | stop it, keeping the data (`docker compose down -v` destroys `pg_data`) |
| `make logs` | follow the app log |
| `make migrate` | `alembic upgrade head` in a one-shot container |
| `make ingest` | load `data/uktzed_hierarchical.json` (idempotent per content hash) |
| `make user USERNAME=…` | create a login and print a generated password once |
| `make test` / `make lint` / `make fmt` | pytest, ruff check + format check, autoformat |
| `make spike` | the M0 smoke app |

---

## Architecture summary

```
        :80 :443 :443/udp
              │
        ┌─────▼─────┐  caddy:2-alpine   TLS, CSP, SSE passthrough
        │   caddy   │  volumes: caddy_data (ACME — back this up), caddy_config, caddy_logs
        └─────┬─────┘
              │  internal docker network, plain HTTP, NO published port
        ┌─────▼─────┐  app     uvicorn --workers 2, stop_grace_period 90s
        │    app    │          FastAPI: /login /chatkit /api/config /history /healthz
        └─────┬─────┘          + the Vite bundle from web/dist
              │
        ┌─────▼─────┐  postgres:17      volume: pg_data
        │     db    │  one database: chat threads, users, tariff, classification records
        └───────────┘
        ┌───────────┐
        │  migrate  │  one-shot `alembic upgrade head`, then exits
        └───────────┘  app depends_on: service_completed_successfully
```

**A request.** The browser POSTs to same-origin `/chatkit` with the session cookie.
FastAPI builds a `RequestContext(user_id, request_id, locale, ledger)` and hands it to
`ChatKitServer.process()`. That context is the entire multi-user boundary: `Store` and
`ChatKitServer` are both `Generic[TContext]`, every one of the 12 `Store` methods
receives it and filters on `user_id`, and "not found or not yours" is always a 404 —
never a 403, so there is no existence oracle. **`ChatKitServer` itself performs zero
authorization.**

`respond()` runs one `Agent` through `Runner.run_streamed` and maps the result into
ChatKit thread events, with the live drill-down rendered as a `Workflow` of tasks
rather than a static "processing…" string. The run ends on a terminal tool
(`emit_classification` / `ask_clarification`) via a `ToolsToFinalOutputFunction` — not
`StopAtTools`, which matches on the tool **call** and would therefore end the run even
when the gate *rejected* the emission.

**Decisions worth knowing before you read the code:**

| | |
|---|---|
| One agent, no handoffs | every handoff is a turn, and `stream_agent_response` drops agent-update events entirely — invisible latency |
| Terminal tools, not `output_type` | `output_type` JSON streams verbatim into the chat bubble |
| Sections are not addressable | all 21 section codes are also valid group codes; 2 digits always means group, section is derived |
| `full_path` is materialised and non-null | 2,473 of 10,490 leaf descriptions are literally "інші" — nothing may read a bare description |
| `is_terminal` is an authored column | `len(code) == 10` happens to agree on this snapshot; that is a property of the file, not the nomenclature |
| The stream always returns `200 text/event-stream` | a 5xx **or** a rewritten content type causes 5 silent client retries, ~25s of dead air and 5× the bill |
| Tracing off | v1 shipped 5,482 trace batches offsite with zero consumers, including customer product descriptions |
| Tree-walk retrieval first | production-proven at 98.7% use and zero data errors; embeddings are M4, and `full_path` is already stored so they need no re-ingest |

Deliberately **not** built: OpenTelemetry, Prometheus, an LLM judge, a reranker, CQRS,
Kubernetes, attachments. The `classification` table *is* the metrics table.

Layout: `app/chat/` (ChatKit integration) and `app/agent/` (the classifier) are separate
so each is replaceable — you can smoke-test `respond()` with a fake model, and run the
agent from the eval harness with no HTTP layer. `data/uktzed_hierarchical.json` is
**committed** (2.4 MB); v1 gitignored it and became a wrapper around a file nobody could
version.

---

## Operations

**Streaming is the whole product difference, and the proxy is where it dies.** Read the
comments in `Caddyfile` before changing it. The short version: a 60-second SSE response
with no heartbeat frames needs an infinite write timeout, no buffering and no
compression on `text/event-stream`. nginx's defaults break both of those; Caddy's do
not, which is why it is here. A failure *after* response headers are sent is fatal and
never retried.

**Cancel is client-side only.** ChatKit never tells the server; the browser just aborts
the stream. So `asyncio.CancelledError` must be allowed to propagate out of the
generator, and the proxy must not buffer — a buffering proxy holds the upstream
connection open, the backend never sees the disconnect, and the agent runs to
completion **and you pay for it**.

**Secrets are a `.env` file, `chmod 600`, root-owned, gitignored.** That is the honest
level for a single-VPS demo: the real trust boundary is SSH access, and anyone who has
that has `docker inspect` too. Docker secrets, Vault or SOPS here would be security
theatre. The two rules that do matter: never bake a secret into an image layer (the
Dockerfile's only `ARG` is `APP_VERSION`), and never log the request body of `/chatkit`.
Rotating `SESSION_SECRET` logs everyone out — that is the break-glass.

**Back up two things.** Postgres, nightly, verified before rotation:

```bash
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$DEST/uktzed-$STAMP.dump"
pg_restore --list "$DEST/uktzed-$STAMP.dump" > /dev/null   # fail loudly on a zero-byte lie
find "$DEST" -name 'uktzed-*.dump' -mtime +14 -delete
rclone copy "$DEST" remote:uktzed-backups --max-age 25h    # OFF the VPS
```

And the `caddy_data` volume — it holds the ACME account key and your certificates.
Losing it is not fatal, but Let's Encrypt rate-limits duplicate certificates to **5 per
registered domain per week**, so a redeploy loop after losing it can lock you out of TLS
for days. Restore into a throwaway container once; an untested backup is not a backup.

**Deploy.**

```bash
cd /srv/uktzed && git pull && docker compose build && docker compose up -d
docker compose logs -f app --tail=100
```

`stop_grace_period: 90s` is not cosmetic. Docker's default is 10 seconds, which would
SIGKILL every in-flight classification on every redeploy.

**Cloudflare: DNS-only (grey cloud).** The orange cloud buffers a response prefix for
WAF inspection, and Error 524 fires if the origin does not respond within 125 seconds.
Whether that 125s resets per SSE chunk is undocumented and unverified — which is exactly
why the recommendation stands.

---

## Pre-deploy checklist

The domain key is the single most likely thing to break launch day, so it gets its own
list:

```
[ ] Hostname registered at platform.openai.com/settings/organization/security/domain-allowlist
    — DAYS ahead, not on the day (propagation is minutes to ~30 minutes)
[ ] domain_pk_... set as CHATKIT_DOMAIN_KEY in the deploy .env
[ ] UKTZED_DOMAIN and ACME_EMAIL set in .env; DNS A/AAAA already pointing at the VPS
[ ] PUBLIC_BASE_URL is the https:// URL (settings.is_production keys off this)
[ ] Loaded the real https:// hostname in a browser with the DevTools console open
[ ] Console shows NO "Domain verification failed"
[ ] Console shows NO "Domain verification skipped" — that means the check never ran
[ ] Console shows NO dev_local_warning — verified:true WITH that field warns but does
    NOT unmount, so "the widget appeared" is not proof the key is right
[ ] No CSP violations in the console (Caddyfile's CSP has never been loaded in a browser)
[ ] Confirmed with at least one end user on their own network (corporate proxy check)
[ ] SSE verified end to end: curl the streaming endpoint through Caddy and watch frames
    arrive incrementally, not in one burst
[ ] Content-Type on the streaming response is text/event-stream and nothing rewrites it
```
