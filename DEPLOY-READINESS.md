# Deploy readiness — uktzed-v2 @ 566d5db

Read-only release audit, 2026-09-17. Scope: a single Ubuntu VPS, Docker Compose, Caddy TLS,
opened as a PUBLIC demo (no login) and later flipped to private from `/backofficeadminpanel`.
Every claim below was re-verified against this checkout; file:line anchors and the exact
command that proves each one are given. Four reviewers filed 20 findings; the section
"What survived re-verification" says what happened to each.

---

## VERDICT: GO-WITH-CHECKLIST

**Nothing in the repo stops the demo from working, but three shipped defaults/docs let a
correct-looking `.env` come up broken (403 on every message, or a chat box that silently
removes itself), and one real code defect makes every abandoned turn run to completion on
your bill with nothing recorded — so deploy only by the numbered checklist below, and fix
M1 before you flip to public if you can spare an hour.**

No BLOCKER was found. The stack builds, migrates, boots, gates on ingest, and streams SSE
through Caddy (all measured in `DEPLOYMENT-VERIFIED.md`); every failure below is either a
configuration the checklist sets explicitly or a defect that costs money/records, not the
demo itself.

---

## What survived re-verification

Reviewers filed 7 MUST-FIX items. Each was reproduced here before being believed.

| Filed | Claim | Result | Lands as |
|---|---|---|---|
| 1 | `/openapi.json` public, lists every `/api/admin/*` route | Reproduced (`TestClient(app, base_url="https://demo.example.com").get("/openapi.json")` → 200, 7 admin paths). **But** `grep -oE '/api/admin[A-Za-z0-9/_{}$-]*' web/dist/assets/index-*.js` already returns `/api/admin/models`, `/overview`, `/settings`, `/users`, `/users/${i}` — the public JS bundle ships the same route table, and `AdminSignIn.tsx:12-17` states the panel's existence is an accepted cost. Marginal leak is request schemas only. | **Downgraded → SHOULD S1** |
| 2 | `POST /api/login` unthrottled: password guessing + CPU/RAM amplifier | Reproduced mechanics (`verify_password` 25 ms/call, t=3 m=64 MiB p=4; `grep -i 'sleep\|attempt\|lockout' app/auth/routes.py` → nothing). **But** `create-user` without `--password` generates `secrets.token_urlsafe(18)` (144 bits, `app/cli.py:125`) — guessing that is not feasible at any rate. The amplifier is real but in the same class as the accepted no-rate-limit exposure on `/chatkit`, which burns model money per request and is strictly worse. | **Downgraded → SHOULD S2**, plus checklist rule "never pass `--password`" |
| 6 | README quickstart creates a non-superuser and never says the app boots private | Reproduced: `README.md:105` → `make user`; `Makefile:46` has no `--superuser`; `grep -n "backofficeadminpanel\|superuser\|private\|access_mode" README.md` → 0 hits. `runtime_settings._resolve_row('access_mode', None).value` → `'private'`. | **MUST-FIX M4** (docs) |
| 7 | `PUBLIC_BASE_URL` must equal `https://$UKTZED_DOMAIN`; quickstart omits it; nothing cross-checks | Reproduced: with the shipped default, `allowed_origins()` → `{'http://127.0.0.1:8000','http://[::1]:8000','http://localhost:8000'}`; `grep -rn UKTZED_DOMAIN app/` → nothing. Also found: an explicit `:443` or any uppercase letter in the value produces an origin a browser never sends (`https://x.example:443`, `https://X.Example`) → 403 on every POST. | **MUST-FIX M2** (docs + one log line) |
| 13 | Closing the tab / stop does not stop the model run; row stays `pending`; reply never persisted | Reproduced end to end. `uvicorn/protocols/http/httptools_impl.py:228` advertises ASGI `spec_version "2.3"`, so Starlette 1.6.0 uses the task-group + `listen_for_disconnect` path (`starlette/responses.py:271-280`) and cancels the scope; the request task is suspended in `asyncio.wait` inside `chatkit/agents.py:239-243`, which never cancels its inner `__anext__` task; the SDK's own `except CancelledError: self.cancel()` lives inside that inner task (`agents/result.py:1017-1021`) and never fires; `app/chat/server.py:244-251` calls `result.cancel()` only under `except Exception`, which cannot catch `CancelledError`. Proof script (real `_merge_generators`, consumer cancelled after 2 events): `produced at cancel=2, produced 1.5s later=3, producer saw CancelledError=False`. | **MUST-FIX M1** (code) |
| 18 | Domain-verification failure reaches `onError` and is thrown away; empty card, no message | Host side reproduced by reading: `ChatKitPanel.tsx:190-195` only `console.error`s; `:100` `notifyStatus` is a no-op because `ChatPage.tsx:15` passes no `onStatusChange`; `:205` always renders `<ChatKit>`. The CDN-side unmount is documented by the author (`README.md:37-41`) and was traced by the reviewer in the live bundle; not re-fetched here. It only matters once something else is already broken and cannot be fixed from the page — it makes the failure *legible*, not absent. | **Downgraded → SHOULD S6** (merged with 20) |
| 19 | Placeholder `CHATKIT_DOMAIN_KEY` passes production boot | Reproduced: `Settings(_env_file=None, public_base_url='https://x.example')` → `is_production=True`, `chatkit_domain_key='domain_pk_localhost_dev'`, no error; `app/main.py:95-96` guards only `SESSION_SECRET`. | **MUST-FIX M3** (merged with 8) |

Dedupes: 8+19 → M3; 9+17 → S7; 18+20 → S6; 4+16 → S9/N4. Nothing was dropped as
unreproducible. One correction to finding 13's text: `app/chat/errors.py:104-105` *does*
map `CancelledError` by name to `"cancelled"`; it is simply unreachable from `respond()`
because `except Exception` never sees a `BaseException`.

---

## MUST-FIX

### M1 — An abandoned turn runs to completion on your bill and is recorded as nothing

**What.** Tab closed or ChatKit's stop button pressed mid-turn: the HTTP request is
cancelled, the OpenAI run is not. All remaining model turns, tool calls and hosted web
search execute and are billed; the assistant reply is never persisted (`ThreadItemDoneEvent`
goes to a queue nobody reads); the `classification` row stays `pending` with NULL tokens, so
the dashboard under-counts exactly the wasted spend. This contradicts the author's own stated
design in `README.md:253-257` and the Caddyfile comment at `Caddyfile:148-153`, both of
which assume the disconnect stops the run.

**Proof.**
- Chain: `starlette/responses.py:271-280` (spec 2.3 → cancel scope) → `chatkit/agents.py:239-243` (`asyncio.wait`, does not cancel inner tasks) → `agents/result.py:1017-1021` (cancel lives in the inner task) → `app/chat/server.py:244-251` (`result.cancel()` only inside `except Exception`) → `:259-318` (`finally` reaches `await finish_turn/fail_turn`, which anyio re-cancels; comment at `:295-298` says the row is "deliberately left pending").
- `grep -n "result.cancel()\|except asyncio.CancelledError" app/chat/server.py` → one hit, line 251, inside `except Exception`.
- `.venv/bin/python -c "import uvicorn;print(uvicorn.__version__)"` → 0.53.0; `grep -n spec_version .venv/lib/python3.13/site-packages/uvicorn/protocols/http/httptools_impl.py` → `"2.3"`.
- Runnable: the reviewer's script at `<scratchpad>/prove_cancel.py` (drives the real `chatkit.agents._merge_generators`, cancels the consumer after two events, asserts the producer keeps producing and never sees `CancelledError`). Output reproduced this session.

**Minimal fix** (`app/chat/server.py`, before line 244):
```python
except asyncio.CancelledError:
    error_class = "cancelled"
    with suppress(Exception):
        result.cancel()                      # stops run_loop_task; sync
    # The current task is inside a cancelled anyio scope: every await here re-raises.
    # A fresh task is not, so the row can still be closed.
    asyncio.get_running_loop().create_task(fail_turn(
        classification_id, error_class="cancelled", tool_calls=ledger.calls,
        turns=result.current_turn, repairs=agent_ctx.repairs,
        tokens_in=result.context_wrapper.usage.input_tokens,
        tokens_cached=result.context_wrapper.usage.input_tokens_details.cached_tokens,
        tokens_out=result.context_wrapper.usage.output_tokens,
        ttfb_ms=_ms_between(started, first_event_at), duration_ms=_ms_since(started),
    ))
    raise
```
and in the `finally`, skip the `finish_turn/fail_turn` awaits when `error_class == "cancelled"`.
Add one test to `tests/test_chat_wiring.py` using the existing `_FakeRun.cancelled`
(`tests/test_chat_wiring.py:109-112`): cancel the consumer task, assert `cancelled is True`.
Keep a reference to the created task (module-level set) so it is not garbage-collected.

**If you ship without it:** accept that every "typed, got bored, closed" visitor costs a
full classification, and read the OpenAI usage page rather than the panel for real spend.

### M2 — `PUBLIC_BASE_URL` is the deploy-day trap and the docs route you straight into it

**What.** `require_same_origin` (`app/auth/deps.py:211-232`) compares the browser's
`Origin` header against exactly one string derived from `PUBLIC_BASE_URL`
(`:197-208`). The shipped default `http://localhost:8000` (`.env.example:21`,
`app/settings.py:39`) is never mentioned in the README quickstart table (`README.md:85-90`;
the only mention is the closing checklist at `:307`), lives in a different `.env` section
from `UKTZED_DOMAIN` (`.env.example:18-21` vs `:24-28`), and the app never reads
`UKTZED_DOMAIN` so nothing can warn. `DEPLOYMENT-VERIFIED.md` already hit this exact wall.
Same default also leaves `is_production` False (`app/settings.py:52-53`), which silently
switches off the `Secure` cookie flag (`app/main.py:110`) and the empty-secret refusal
(`:95-96`).

**Proof.**
```
PUBLIC_BASE_URL=http://localhost:8000 UKTZED_DOMAIN=x.example .venv/bin/python -c \
  "from app.auth.deps import allowed_origins; print(sorted(allowed_origins()))"
# ['http://127.0.0.1:8000', 'http://[::1]:8000', 'http://localhost:8000']
grep -rn UKTZED_DOMAIN app/        # no output
```
Value shape matters: `https://x.example/` → OK; `https://x.example:443` → origin
`https://x.example:443` (browsers omit the default port → 403); `https://X.Example` →
`https://X.Example` (browsers send lowercase → 403). Verified by calling `allowed_origins()`
with each.

**Minimal fix.** `.env.example`: move `PUBLIC_BASE_URL` directly under `UKTZED_DOMAIN`
with the comment `# MUST be https://<UKTZED_DOMAIN>, lowercase, no port, or every POST is
403`. `README.md:85-90`: add the row. `app/main.py` lifespan: one line,
`logger.info("allowed origins: %s", sorted(allowed_origins()))`, so `make logs` shows the
mismatch. The checklist below already does the check by hand (step 7).

### M3 — Production boot passes with the placeholder domain key; visitors get an empty chat and the server logs nothing

**What.** `CHATKIT_DOMAIN_KEY` defaults to `domain_pk_localhost_dev` (`app/settings.py:34`,
`.env.example:16`) and is served verbatim by `GET /api/config` (`app/chat/routes.py:162-163`).
The only production guard is for `SESSION_SECRET` (`app/main.py:95-96`). On a real
`https://<domain>` the ChatKit frame verifies the key against the org allowlist and
**unmounts itself** on any answer other than `verified: true` (`README.md:37-41`). `/healthz`
and `/readyz` never look at this value, so Docker says healthy, curl streams fine, and every
browser visitor sees the header and an empty card.

**Proof.**
```
.venv/bin/python -c "from app.settings import Settings; s=Settings(_env_file=None, \
  public_base_url='https://x.example'); print(s.is_production, s.chatkit_domain_key)"
# True domain_pk_localhost_dev
grep -rn "domain_pk_localhost_dev" app/    # only the default in settings.py:34
```

**Minimal fix.** Next to the `SESSION_SECRET` guard in `app/main.py` (a second function or
two lines in `_session_secret`'s neighbourhood):
```python
if settings.is_production and settings.chatkit_domain_key in ("", "domain_pk_localhost_dev"):
    raise RuntimeError("CHATKIT_DOMAIN_KEY is the dev placeholder; set the real domain_pk_ key")
```
The container then refuses to start instead of the visitor's console being the only witness.

### M4 — The README bootstraps the wrong kind of user and never says the app is closed by default

**What.** `README.md:105` says `make user USERNAME=anton`; `Makefile:46` runs `create-user`
with no way to pass `--superuser`. The app resolves `access_mode` to `private` when the
table is empty (`app/runtime_settings.py` docstring; `_resolve_row('access_mode', None)` →
`'private'`), so a deployer who follows the README ends with a stack that is up, a tariff
ingested, a login page, and a user for whom every `/api/admin/*` call 404s
(`app/auth/deps.py:177-179`). There is no UI path to self-promote; the only route is
`create-user … --superuser` / `grant-superuser`, documented solely in `ADMIN-MILESTONE.md:221-240`.

**Proof.** `grep -n "backofficeadminpanel\|superuser\|private\|access_mode" README.md` → no
output. `sed -n 46p Makefile`. `grep -n '@cli.command' app/cli.py` (the commands that exist:
`create-user`, `grant-superuser`, `revoke-superuser`, `purge-guests`, `set-password`,
`disable-user`, `ingest`, `list-users`).

**Minimal fix.** `README.md` step 2: replace the `make user` line with
`docker compose exec app python -m app.cli create-user anton --superuser`, add a
`superuser:` Makefile target, and add the two sentences: "The app starts PRIVATE. Nobody can
use it until a superuser signs in at `/backofficeadminpanel` and flips access mode to
public." (The checklist below already uses the right command.)

---

## Pre-deploy checklist — run it in this order

Placeholders: `DOMAIN` = your hostname (lowercase), `/srv/uktzed` = clone path. Everything
runs as the user who owns `/srv/uktzed` and is in the `docker` group, unless stated.

### A. Days ahead (nothing here touches the VPS)

1. **OpenAI domain allowlist.** At `platform.openai.com/settings/organization/security/domain-allowlist`
   register `DOMAIN` and copy the resulting `domain_pk_…` key. Propagation was reported at
   minutes to ~30 minutes (`README.md:43-47`); do it days ahead anyway because the check
   has *never* been exercised by this codebase (it is skipped on localhost).
2. **OpenAI billing limit.** In the OpenAI dashboard (Organization → Limits / Budgets) set a
   monthly hard limit you are willing to lose. In-app caps and rate limits were declined by
   decision; this dashboard limit is the *only* remaining spend control and costs nothing.
   Note factually: with M1 unfixed, every abandoned turn bills in full, and `OPENAI_TELEMETRY`
   defaults to `true` (`app/settings.py:31`), so traces and request bodies are retained at
   OpenAI unless you set it `false`.
3. **DNS.** Create `A` (and `AAAA` if the VPS has v6) for `DOMAIN` → VPS IP. If Cloudflare:
   **grey cloud / DNS-only** (`README.md:290-293`). Confirm before step C: `dig +short DOMAIN`
   returns the VPS IP. Starting Caddy before DNS resolves burns Let's Encrypt validation
   attempts (5 failures per hostname per hour).

### B. VPS preparation

4. Install Docker Engine + the compose plugin (`docker compose version` → v2.x). Open the
   firewall: `sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw allow 443/udp && sudo ufw enable`.
   Port 80 must be open: Caddy uses it for the HTTP→HTTPS redirect and ACME.
5. Clone at the audited commit:
   ```bash
   sudo mkdir -p /srv/uktzed && sudo chown "$USER" /srv/uktzed
   git clone <repo-url> /srv/uktzed && cd /srv/uktzed && git checkout 566d5db
   ```

### C. Configuration — the step that decides whether the demo works

6. Create and lock `.env`:
   ```bash
   cp .env.example .env && chmod 600 .env
   ```
   Edit these, **all of them**, in `.env`:
   ```dotenv
   OPENAI_API_KEY=sk-...                       # required; nothing at boot checks it (see 5-minute check 8)
   OPENAI_TELEMETRY=true                       # or false — decide (step 2)
   CHATKIT_DOMAIN_KEY=domain_pk_...            # from step 1. NOT domain_pk_localhost_dev (M3)
   SESSION_SECRET=<openssl rand -base64 48>    # required; empty is refused only when PUBLIC_BASE_URL is https (M2/S7)
   POSTGRES_PASSWORD=<openssl rand -hex 32>    # hex, not base64: '/', '+', '=' break the URL below
   DATABASE_URL=postgresql://postgres:<same password>@db:5432/uktzed
   UKTZED_DOMAIN=DOMAIN                        # lowercase hostname only
   PUBLIC_BASE_URL=https://DOMAIN              # EXACTLY https:// + UKTZED_DOMAIN. No :443, no uppercase (M2)
   ACME_EMAIL=you@yourdomain                   # any real address
   ```
   `POSTGRES_PASSWORD` is applied only at the first `initdb`; changing it later does not
   change the database password (`docker-compose.yml:143`).
7. Prove the three values agree **before** starting anything:
   ```bash
   set -a; . ./.env; set +a
   [ "$PUBLIC_BASE_URL" = "https://$UKTZED_DOMAIN" ] && echo ORIGIN-OK || echo "ORIGIN-MISMATCH: every POST will be 403"
   [ "$CHATKIT_DOMAIN_KEY" != "domain_pk_localhost_dev" ] && [ -n "$CHATKIT_DOMAIN_KEY" ] && echo KEY-OK || echo "KEY-IS-PLACEHOLDER: chat will unmount"
   [ -n "$SESSION_SECRET" ] && echo SECRET-OK || echo "SECRET-EMPTY: boot will refuse"
   echo "$PUBLIC_BASE_URL" | grep -qE '[A-Z]|:443' && echo "BAD-ORIGIN-SHAPE" || echo SHAPE-OK
   ```
   All four must print `-OK`.

### D. Bring-up (still private — nobody outside can use it yet)

8. Build and start:
   ```bash
   docker compose build --build-arg APP_VERSION="$(git describe --always --dirty)"
   docker compose up -d
   docker compose ps          # expect: migrate Exited (0); app Up (healthy); caddy Up; db Up (healthy)
   docker compose logs caddy | grep -iE 'certificate obtained|error'   # want "obtained", no ACME errors
   ```
   If `app` restart-loops: `docker compose logs app --tail=50` — a `RuntimeError: SESSION_SECRET`
   means step 6; an asyncpg auth error means `DATABASE_URL` and `POSTGRES_PASSWORD` disagree.
9. Ingest the tariff (idempotent):
   ```bash
   docker compose run --rm app python -m app.cli ingest data/uktzed_hierarchical.json
   # expect 14187 nodes / 10490 terminals
   curl -s https://DOMAIN/readyz      # expect {"status":"ok",...,"tariff_nodes":14187,...}
   ```
10. Create the first superuser — **do not pass `--password`**; use the generated one
    (`/api/login` has no throttle, S2; a generated 144-bit password makes that moot):
    ```bash
    docker compose exec app python -m app.cli create-user anton --superuser
    # copy the printed password: it is shown once
    docker compose exec app python -m app.cli list-users   # confirm is_superuser
    ```

### E. Verify while still private (curl only; then one browser)

11. Origin gate (this is the M2 probe, no browser needed — `/api/logout` needs only a
    same-origin header and answers 204):
    ```bash
    curl -s -o /dev/null -w '%{http_code}\n' -X POST https://DOMAIN/api/logout -H "Origin: https://DOMAIN"
    # 204 = PUBLIC_BASE_URL is right.  403 = fix .env, docker compose up -d app
    ```
12. Config the browser will see:
    ```bash
    curl -s https://DOMAIN/api/config
    # "access_mode":"private", "domain_key":"domain_pk_<yours>", "user":null
    ```
13. Browser, DevTools console open: `https://DOMAIN/backofficeadminpanel` → sign in →
    the overview renders and access mode reads «приватний». If sign-in says «Невірне ім'я…»
    with a 403 in the Network tab, that is M2; if it signs in but the panel 404s, the user is
    not a superuser (M4).
14. Optional SSE probe from the shell, one model call, before any stranger does it. Requires
    public mode (step 15) or a logged-in cookie jar; the body is a valid `threads.create`
    (validated against chatkit 1.6.5 types):
    ```bash
    J=/tmp/uktzed.jar; curl -s -c $J -b $J https://DOMAIN/api/config >/dev/null
    curl -sN -b $J -H "Origin: https://DOMAIN" -H "Content-Type: application/json" \
      -X POST https://DOMAIN/chatkit \
      -d '{"type":"threads.create","params":{"input":{"content":[{"type":"input_text","text":"Чоловіча футболка, 100% бавовна"}],"attachments":[],"quoted_text":null,"inference_options":{}}}}' \
      | while IFS= read -r l; do printf '%s %s\n' "$(date +%T)" "${l:0:90}"; done
    # frames must arrive over ~10-30 s, not in one burst at the end
    ```

### F. Open it

15. In the panel, flip access mode to «публічний». Confirm from the shell:
    `curl -s https://DOMAIN/api/config | grep -o '"access_mode":"[a-z]*"'` → `public`.
16. Private/incognito window, console open, `https://DOMAIN/`: the start screen (greeting +
    three prompts) appears **and is still there 20 seconds later**. Console must show none of:
    `Domain verification failed`, `Domain verification skipped`, `dev_local_warning`,
    `Content Security Policy` (`README.md:308-313`). Then run one prompt to completion and
    open `/history`.
17. Send the URL to one person on a corporate/VPN network and ask whether the chat box
    appears (`README.md:58-61`: their browser must reach `api.openai.com`).

### G. After opening (keep these at hand)

- **Kill switch:** panel → access mode → «приватний». Guests are logged out on their next
  request (`app/auth/deps.py:153-155`); the superuser stays in.
- **Break-glass kill switch when the panel itself is unreachable** (deleting the override
  row makes the app fall back to its fail-closed default, `_resolve_row('access_mode', None)` → `private`; the value is read uncached on every request):
  ```bash
  cd /srv/uktzed && set -a && . ./.env && set +a
  docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DELETE FROM app_setting WHERE key='access_mode';"
  ```
- **Backup** (the README snippet does not run as written, S8):
  ```bash
  cd /srv/uktzed && set -a && . ./.env && set +a
  DEST=/var/backups/uktzed; STAMP=$(date -u +%F); sudo mkdir -p "$DEST"
  docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$DEST/uktzed-$STAMP.dump"
  pg_restore --list "$DEST/uktzed-$STAMP.dump" >/dev/null && echo backup-ok
  # restore:
  docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists < "$DEST/uktzed-$STAMP.dump"
  ```
  Also back up the `caddy_data` volume (ACME account + certs; `README.md:275-278`).
- **Redeploy:** `git pull && docker compose build && docker compose up -d` (`README.md:283`).
  `stop_grace_period: 90s` covers in-flight turns; anything longer is left `pending` (S4).
- **Guest hygiene:** `docker compose exec -T app python -m app.cli purge-guests --older-than-days 30 --yes` (cron it, N4).

---

## SHOULD — before a second demo

- **S1. `/openapi.json` is public and enumerates the admin API** (filed 1, downgraded).
  `app/main.py:101` sets `docs_url=None, redoc_url=None` but not `openapi_url=None`.
  Proof: `TestClient(app, base_url="https://demo.example.com").get("/openapi.json")` → 200,
  paths include all seven `/api/admin/*`. The bundle already leaks the route table, so the
  marginal exposure is request schemas; the fix is one kwarg (`openapi_url=None`), and
  `tests/test_chat_wiring.py:692` calls `app.openapi()` directly so it keeps passing.
- **S2. `POST /api/login` has no lockout and no concurrency bound** (filed 2, downgraded).
  `app/auth/routes.py:52-66`; `app/auth/passwords.py:21` (argon2id m=64 MiB); on a 2-vCPU
  box `asyncio.to_thread` allows 6 threads × 2 workers = up to 768 MiB of concurrent
  verifies from an anonymous POST loop. Not a rate limit: a module-level
  `asyncio.Semaphore(2)` around the `to_thread` call bounds RAM/CPU without rejecting
  anyone. Guessing is moot while the password is the generated one (step 10).
- **S3. No request-body size limit on the path** (filed 3). `Caddyfile` has no
  `request_body { max_size }` (`grep -c request_body Caddyfile` → 0); `app/chat/routes.py:99`
  buffers the whole body. Add `request_body { max_size 1MB }` in the site block.
- **S4. `pending` is terminal; nothing sweeps rows abandoned by a tab close, redeploy or
  worker SIGKILL** (filed 14). `grep -rn 'UPDATE classification' app | grep -v _CLOSE_TURN`
  → nothing; the only closer runs in the request task. One idempotent `UPDATE … SET
  outcome='error', error_class='cancelled', finished_at=now() WHERE outcome='pending' AND
  created_at < now() - interval '15 minutes'` in `lifespan()` after `init_pool`. M1's fix
  closes the tab-close case; this closes the rest.
- **S5. `ModelSettings.timeout=90` is a hard deadline per model call and a timeout is
  reported as «Внутрішня помилка» / `error_class='internal'`** (filed 15). Proof:
  `classify_error(ModelTimeoutError(90.0))` → `internal`. `app/agent/agent.py:105`;
  `app/chat/errors.py:101-136` has no `ModelTimeoutError` branch. The panel offers
  `xhigh`/`max` effort (`app/runtime_settings.py:70`); a single high-effort call with web
  search can exceed 90 s and then every turn fails with the wrong message. Add the branch
  (→ `"timeout"`) and raise the deadline toward `turn_wall_clock_s` (180 s, `app/settings.py:48`).
- **S6. No visible fallback when the chat unmounts or never mounts** (filed 18+20).
  `ChatKitPanel.tsx:190-195, 100, 205`; `ChatPage.tsx:15`; `web/index.html:25` has no
  `onerror`; `@openai/chatkit-react` awaits `customElements.whenDefined` with no timeout.
  A domain-key failure, a blocked `api.openai.com`, or a blocked CDN all look identical:
  header + empty card, nothing in any server log. Keep a `mountError` state; set it from
  `onError` for `IntegrationError`/`DomainVerificationRequestError` and from a
  `Promise.race([customElements.whenDefined('openai-chatkit'), 8 s])` watchdog; render a
  Ukrainian message with a reload button in place of `<ChatKit>`.
- **S7. Empty `SESSION_SECRET` with `--workers=2` gives each worker its own signing key —
  but only when `PUBLIC_BASE_URL` is not https** (filed 9+17). Proof:
  `_session_secret(Settings(session_secret='', public_base_url='http://203.0.113.5'))`
  differs per call; `app/main.py:96-98,106` evaluates it at import, once per worker
  process. Symptom: a guest is re-minted on every other request and history flickers. Make
  the empty-secret branch always raise; step 6 already sets it.
- **S8. README backup snippet does not run as written and there is no restore** (filed 10).
  `README.md:269-272` uses `$POSTGRES_USER`, `$POSTGRES_DB`, `$DEST`, `$STAMP`, none of which
  exist in the host shell. The working version is in section G above; put it in the README.
- **S9. A cross-origin or cookie-less `POST /chatkit` mints a guest row before it is
  rejected 403** (filed 4). `app/chat/routes.py:84-86`: `Depends(current_user)` runs before
  `require_same_origin(request)`. Swap the order (call `require_same_origin` first, then
  `user = await current_user(request)`).

## NICE

- **N1.** `items.feedback` and `attachments.create` ops answer 500 with a full traceback in
  the app log on every probe (`app/chat/server.py:338-349` raises `NotImplementedError`;
  `app/chat/routes.py:100` converts only `ValidationError`). Catch `(NotImplementedError,
  RuntimeError)` → 400.
- **N2.** `README.md:92-97` and `docker-compose.yml:5-8` claim `UKTZED_DOMAIN`/`ACME_EMAIL`
  are not in `.env.example`; they are (`.env.example:27-28`). Delete the paragraph.
- **N3.** `caddy:2-alpine` floats across minor versions (`docker-compose.yml:33`); the
  Caddyfile's `servers { timeouts }` and `encode { match }` syntax is version-sensitive.
  Pin to the minor you verified with (`docker compose images`).
- **N4.** Guest rows are never purged automatically; every cookie-less hit on `/api/config`
  inserts one (`app/auth/guest.py:82-92`; `app/auth/deps.py:129-131` acknowledges it).
  Cron: `0 4 * * * cd /srv/uktzed && docker compose exec -T app python -m app.cli purge-guests --older-than-days 30 --yes`.

Noted once, factually, per the author's decision: there is no spend cap and no rate limit
anywhere on `/chatkit`; any visitor can start as many classifications as they like, and with
M1 unfixed each one bills in full even if abandoned. The OpenAI dashboard limit (step 2) is
the only control.

---

## What CANNOT be verified before the first real HTTPS deploy

1. **The domain-key gate.** Skipped on localhost/non-443; has never run for this codebase.
   Which of its four failure branches fires depends on the key, allowlist propagation and the
   visitor's network.
2. **The CSP in a real browser against the live CDN frame.** `README.md:313` says so; the
   compose run in `DEPLOYMENT-VERIFIED.md` was curl-only.
3. **Let's Encrypt issuance** for `DOMAIN` (DNS, port 80/443 reachability, rate limits).
4. **Client-disconnect propagation through Caddy to uvicorn.** The Caddyfile comment at
   `:148-153` asserts the default flush behaviour cancels the upstream request; it was not
   measured. Even when it propagates, M1 means the model run continues — what you can
   observe is only *when* the app logs the turn.
5. **Two-worker cookie continuity** with a real `SESSION_SECRET` (correct by construction;
   observable only by refreshing on the real host).
6. **Visual rendering** of progress/thinking/widgets (`DEPLOYMENT-VERIFIED.md`, "Still not
   verified").
7. **Visitors on networks that block `api.openai.com` or `cdn.platform.openai.com`.**

## The first five minutes after it is up — in order, and what "broken" looks like

1. `docker compose ps` — migrate `Exited (0)`, app `healthy`, caddy/db `Up`.
   *Broken:* app restarting → `docker compose logs app --tail=50` shows `RuntimeError:
   SESSION_SECRET` (step 6) or asyncpg `password authentication failed` (DATABASE_URL vs
   POSTGRES_PASSWORD).
2. `curl -sI https://DOMAIN/ | head -1` → `HTTP/2 200`; `curl -vI https://DOMAIN 2>&1 | grep -i issuer`
   → Let's Encrypt. *Broken:* connection refused (firewall/DNS); self-signed issuer
   (`UKTZED_DOMAIN` still `localhost`); ACME errors in `docker compose logs caddy`.
3. `curl -s https://DOMAIN/readyz` → `"status":"ok"`, `"tariff_nodes":14187`.
   *Broken:* 503 `not_ready` → step 9 not run.
4. `curl -s https://DOMAIN/api/config` → `domain_key` is yours, not
   `domain_pk_localhost_dev`. *Broken:* placeholder → M3; fix `.env`, `docker compose up -d app`.
5. `curl -s -o /dev/null -w '%{http_code}\n' -X POST https://DOMAIN/api/logout -H "Origin: https://DOMAIN"`
   → 204. *Broken:* 403 → M2 (`PUBLIC_BASE_URL`); the chat would accept typing and fail on
   send.
6. Browser, console open, `/backofficeadminpanel`, sign in, overview renders.
   *Broken:* 403 on `/api/login` in Network → M2; signed in but `/api/admin/overview` 404 →
   user is not a superuser (M4).
7. Flip public. Incognito `/`: start screen appears and **stays** for 20 s; console clean.
   *Broken:* card empties after a few seconds → domain key/allowlist (console:
   `Uncaught IntegrationError: Domain verification failed for https://DOMAIN`); card never
   fills → CDN blocked or CSP (console: `Refused to load…`); `Domain verification skipped`
   → the check did not run at all, which means the origin/port is not what you think.
8. Run one start-screen prompt; frames stream in over 10-30 s; `docker compose logs -f app`
   prints `turn done … outcome=… error=None`. *Broken:* «Внутрішня помилка» immediately →
   `OPENAI_API_KEY` missing/invalid (log shows the OpenAI auth error; nothing at boot checks
   the key, `app/agent/agent.py:75`); «Сервіс ШІ тимчасово недоступний» → upstream/network;
   everything arrives in one burst at the end → something is buffering (Cloudflare orange
   cloud).
9. Refresh five times: the same thread stays open. *Broken:* a fresh empty chat each time →
   cookie not sticking (S7 only on http, or the browser blocks cookies).
10. Close the tab mid-turn; the app log prints `turn done … latency_ms=<seconds>`
    immediately. *If it prints only after the full turn:* disconnects are not propagating
    through the proxy. *Either way,* until M1 is fixed the OpenAI usage page will show the
    full run.
11. `/history` as the guest shows the row; the panel's «Сьогодні» tile counts it.
