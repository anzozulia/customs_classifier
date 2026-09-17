# Milestone: access mode, guest identity, and the hidden admin panel

> "I want to demo this to a wide audience with no login, and then — once the demo is over —
> flip it to login-gated and hand credentials to a few people. Without a redeploy."

Three things had to become true for that, and one thing had to stay true.

1. A runtime **access mode**, `public` or `private`, switchable from an admin panel with no
   restart and no deploy. Flipping to `private` stops guest traffic immediately.
2. In `public` mode every visitor gets **their own history**, surviving a reload and a closed
   browser.
3. A **hidden** admin panel for one superuser: users, model, access mode, reasoning effort,
   and a usage + cost dashboard.

The thing that had to stay true: **no spend caps and no rate limits** — an explicit decision.
The mode toggle is the only lever and the dashboard is the only spend visibility, which is
why the toggle is uncached and the dashboard is computed from the register rather than
estimated.

---

## The central decision

**A guest is a real `app_user` row.** Not a parallel identity, not a nullable `user_id`, not
an "anonymous" branch in the store.

Every isolation boundary this application already had keys on `app_user.id`: the fourteen
`PgStore` methods that filter on `context.user_id`, `classification.user_id`, `/api/history`.
Giving a visitor a real row means all of it keeps working with **zero changes** — two people
on the same demo cannot see each other's conversations for exactly the reason two employees
cannot. The alternative (a second identity type) would have meant auditing every one of those
boundaries for a second code path, which is how isolation bugs get shipped.

The consequences are all small and all deliberate:

| | |
|---|---|
| `username` | `guest_` + `secrets.token_urlsafe(12)` — 16 chars, ~96 bits |
| `display_name` | «Гість» |
| `password_hash` | `GUEST_PASSWORD_SENTINEL = "!guest-no-password-login-impossible"` |
| `kind` | `'guest'`, with a CHECK constraint; `is_superuser` cannot be set for one |
| cleanup | `python -m app.cli purge-guests --older-than-days N` |

The sentinel is not `''` on purpose. An empty string is what a column default or a
half-written INSERT produces *by accident*; this value is obviously intentional and is not a
valid argon2 PHC string, so `verify_password` can only take its `InvalidHashError` branch.
"A guest can never log in" is a property of the row, not a rule someone has to remember.

---

## 1. The access mode

| | |
|---|---|
| stored in | `app_setting` (key TEXT PK, value JSONB, updated_at, updated_by) — migration `0005_admin` |
| default | `private`, hardcoded in `app/runtime_settings.py`, **never** read from the environment |
| read by | `app/auth/deps.py::current_user`, on every request that needs it |
| cached | **never** |

The default is a module constant rather than an env var so that "the demo was open all night
because of a stale line in a `.env` file" is not a state this deployment can reach. Every
failure mode resolves the same way: no row, an unparseable row, a hand-edited value, a
database that will not answer — all of them fail closed to `private`, and the bad row is
logged at WARNING on every read.

`get_access_mode()` is a primary-key lookup on a three-row table, issued on a request that
was already fetching the user row from the same pool in the same millisecond. Two single-row
lookups instead of one buys an exact, worker-wide kill switch with no convergence window to
explain. `model` and `reasoning_effort` are different — they sit on the hot path next to a
60-second model call, so they share a 3-second TTL snapshot cache. Under several uvicorn
workers those two converge within the TTL rather than applying instantly; the demo runs one
worker, where it is exactly zero.

The rule `current_user` enforces, whole:

```
valid session, kind='human'   → that user, in either mode. The mode is never consulted for
                                them, so a superuser cannot be locked out of the switch they
                                just flipped.
valid session, kind='guest'   → that guest in public; in private the session is CLEARED and
                                the request 401s.
no valid session              → a freshly minted guest in public; 401 in private.
```

The guest session is **cleared**, not merely refused, so that flipping back to `public` later
hands out a new identity instead of silently resurrecting one from a cookie the browser was
told was dead.

In public mode a human whose account was disabled, or whose `session_epoch` was bumped, is
demoted to a fresh guest rather than 401'd: the app is open to everyone in that mode anyway,
and they never get the revoked account back.

---

## 2. History that survives the browser closing

The session cookie is the only handle a guest has on their own conversations — no password,
no email, no recovery flow. So `SESSION_MAX_AGE_S` in `app/main.py` is **90 days** (it was
14). At 14 days a visitor who came back after a fortnight silently became a different person
with an empty history, which is the requirement failing quietly rather than loudly.

Verified against the real app (`TestClient`, no database):

```
set-cookie: uktzed_session=…; path=/; Max-Age=7776000; httponly; samesite=lax
```

`Max-Age` is what makes it a persistent cookie rather than a session cookie, i.e. what makes
it survive quitting the browser. `_SLIDING_REFRESH_S` (one day) re-signs it for anyone who
keeps using the app, so 90 days is a floor, not a deadline.

A long cookie is not a weaker cookie here: every request re-reads the row and re-checks
`is_active` and `session_epoch`, so `set-password` and `disable-user` revoke instantly;
flipping to `private` kills guest sessions on their next request; and `purge-guests` deletes
the rows themselves, which cascades to threads, thread items, attachments, classifications,
per-code rows and tool-call rows (the FKs were read, not assumed — migrations 0002 and 0004
declare them `ON DELETE CASCADE`).

**Where a guest is born:** `GET /api/config`, the SPA's first request. It calls
`current_user` and swallows only the 401, so in public mode an anonymous GET mints the row
and `SessionMiddleware` puts the `Set-Cookie` on that same response — the visitor has an
identity before they type anything. The flip side, documented in the route: a crawler that
never returns the cookie leaves one abandoned row per visit. That is what `purge-guests` is
for.

---

## 3. The hidden panel

`/api/admin/*`, eight routes, every one of them behind `require_superuser` — declared **once**
on the `APIRouter`, next to `require_same_origin`, so no route added later can forget either.

**404, never 403.** A 403 confirms that the path exists, which is precisely what a hidden
panel must not do. `require_superuser` raises `404 {"detail": "Not found"}` — byte-identical
to the body `app/main.py` already returns for a store `NotFoundError`, so `/backofficeadminpanel` and
`/nonsense` are indistinguishable on the wire and, in the SPA, pixel-identical (both render
the same `NotFoundPage` inside the same shell).

It also deliberately does **not** go through `current_user`: it resolves the session without
minting, so probing a hidden admin URL anonymously cannot insert rows into `app_user`. And it
is deliberately not mode-aware: a superuser must reach the switch in either mode.

| route | what it does |
|---|---|
| `GET /overview` | access mode, model, effort, prompt version + digest, dataset digest, and usage for today / last 7 days / all time |
| `PUT /settings` | one `{key, value}` per request; 400 with the validator's own sentence on a bad value |
| `GET /users?kind=&limit=` | humans first, then guests by most recent activity |
| `POST /users` | create a human (there is no route that creates a guest) |
| `POST /users/{id}/password` | sets the hash and bumps `session_epoch` — a logout everywhere |
| `POST /users/{id}/active` | disabling revokes; re-enabling does not bump the epoch |
| `POST /users/{id}/superuser` | grant or revoke the panel |
| `GET /models` | the price table from `app/records/pricing.py`, never an OpenAI call |

**The dashboard is computed, never estimated.** Every number is an aggregate over
`classification`, the table that already records one row per turn *including the failed ones*
— exactly the population an estimate built from successes would miss. Three things it says
out loud rather than letting the reader assume the flattering reading:

* `today` means **00:00 UTC** (03:00 Kyiv). Each window reports the `since` it actually used;
  the panel labels the tile «Сьогодні (UTC)» and puts the exact boundary in its tooltip.
* `cost_usd` is a **floor**. A turn on a model that is not in the price table stores
  `cost_usd = NULL`; `runs_unpriced` counts those and the panel prints them in amber as
  excluded from the total.
* a "user" is an account that ran **at least one classification** in that window, not an
  account that exists — so the label is «Активні користувачі», with the human/guest split
  under each number.

---

## 4. What the panel changes, and what actually reads it

This is the part that is easy to fake and worth checking, because v1 shipped a banner naming
`o3` + `gpt-5` while the code constructed `o4-mini` + `gpt-4.1` and the logs lied for two
months. A settings row that nothing reads would be the same bug with a nicer UI.

| key | who reads it, per request/turn |
|---|---|
| `access_mode` | `app/auth/deps.py::current_user` (uncached) and `GET /api/config` |
| `model` | `ClassifierServer.respond()` → `build_agent(prompt, model, effort)`, the `classification` row it opens, the turn-summary log line, and `/readyz` |
| `reasoning_effort` | `model_settings(effort)` on the agent that runs the turn |

`respond()` resolves both values **once** and feeds the same two strings to all three sinks.
`tests/test_chat_wiring.py::test_the_runtime_model_and_effort_reach_the_agent_and_the_record`
asserts that against a real `app_setting` row rather than a patched accessor.

`/readyz` now reports the **effective** model for the same reason.

⚠ **Evals.** `python -m evals.run --model X` sets `MODEL` in the environment, which is the
*default* layer — a `model` row written from the panel beats it. Before a gated eval run
against the same database, clear the override (the panel's model select writes `app_setting`;
`DELETE FROM app_setting WHERE key = 'model'` restores the environment) or run evals against
a database that has none. The observation rows record the model that actually ran; `RunMeta`
still reports `settings.model`, so a stale override would make those two disagree. This is the
one sharp edge this milestone leaves.

---

## 5. Safety rails

There is no shell behind this panel during a demo. A superuser who locks themselves out
cannot get back in without `docker compose exec`, which is the situation the panel exists to
avoid. Each of these is a 409 with a Ukrainian sentence the UI prints verbatim:

* **self-demote** — you cannot remove your own `is_superuser`;
* **self-deactivate** — you cannot disable your own account;
* **the last active superuser** — cannot be demoted and cannot be disabled. This one lives
  *inside* the two guarded `UPDATE` statements rather than in a `SELECT` then an `UPDATE`,
  because the race it exists for is two superusers demoting each other at the same moment:
  both would read "there is another one", both would write, and the demo would end with
  nobody able to reach the panel;
* **guests** cannot be granted superuser and cannot be given a password (that would quietly
  convert a guest row into an account).

Around them: `require_same_origin` on every mutating route (layer 2 behind `SameSite=Lax`),
`session_epoch` revocation on password change and deactivation, and a settings validator that
runs at write time *and* at read time so a value hand-edited into `app_setting` by `psql`
degrades the app to its environment configuration instead of bricking it.

---

## 6. The first superuser

Bootstrap is CLI-only, on purpose: the panel can create more admins, but the first one cannot
come from the network.

```bash
# in the container (or with the venv and DATABASE_URL set)
python -m app.cli create-user anton --superuser      # prints a generated password once
python -m app.cli grant-superuser anton              # promote an existing account
python -m app.cli revoke-superuser anton
python -m app.cli list-users                         # humans; add --guests to include guests
python -m app.cli purge-guests --older-than-days 30  # counts, confirms, deletes (--yes skips)
```

`grant-superuser` / `revoke-superuser` deliberately do **not** bump `session_epoch`:
`require_superuser` re-reads the row on every request, so a revocation lands on the target's
next click without also logging them out of a conversation.

Then: sign in, type `/backofficeadminpanel` (or click the ✦ in the header, which is rendered only for a
superuser), and flip the access mode to `public` when the demo starts.

---

## 7. What this wiring pass changed

The identity layer, the admin API and the admin UI were built in parallel against a written
contract. This pass mounted them and reconciled where the three had drifted.

**Server**

* `app/main.py` — included `admin_router` with the other routers, i.e. **before** the SPA
  mount at `/` (Starlette matches in registration order; a router included after that mount
  is answered by the static handler and is unreachable). Raised `SESSION_MAX_AGE_S` to 90
  days. `/readyz` reports the effective model.
* `app/chat/routes.py` — `GET /api/config` now returns `access_mode` and, on `user`, `kind`
  and `is_superuser`. Still never 401s.
* `app/chat/server.py`, `app/agent/agent.py` — the runtime `model` and `reasoning_effort`
  reach the agent, the record and the log line. `build_agent` is now keyed on all three.
* `app/backofficeadminpanel/routes.py` — added `tokens_cached` and `runs_unpriced` to each usage window, so
  the dashboard can show the cache split and say when its total is a floor.

**Frontend** (`web/src/lib/backofficeadminpanel.ts` was written before the server existed and assumed a
different shape; it is now the adapter, and no component had to learn about the difference)

* overview is flat, not nested under `settings`; the window is `last_7d`, not `week`; turns
  are `classifications`, not `runs`; outcomes and user counts are **per window**;
* `PUT /settings` takes one key per request — `saveSettings` sends one PUT per key and
  returns only what the server confirmed (each value read back out of the database after the
  write, not echoed from the request);
* `GET /models` is a bare array, and stamps `pricing_version` onto every option;
* `GET /users` takes only `kind` and `limit` (no `q`, no `offset`, no total), so the search
  box and «Показати ще» filter and page **client-side** over the most recent 1000 rows of the
  chosen kind. `total` therefore means "matches within that window". The numbers the author
  actually watches are on the dashboard, which is SQL over every row;
* there is no `PATCH /users/{id}`: `updateUser` dispatches to the three narrow POSTs, each of
  which carries its own 409 rail;
* `REASONING_EFFORTS` is `low | medium | high` — what the server validates against. `minimal`
  was a guess and would have been a 400.

---

## 8. Verification

Run on this machine, real output:

| gate | result |
|---|---|
| `.venv/bin/python -m ruff check app tests scripts migrations evals` | **All checks passed!** |
| `.venv/bin/python -m pytest -q` | **374 passed** (201 before this milestone; 362 after the three chunks; +12 in this pass) |
| `.venv/bin/python -c "import app.main"` | imports |
| `.venv/bin/python -m alembic heads` | `0005_admin (head)` — exactly one |
| `cd web && npx tsc --noEmit && npm run build` | passes; bundle rebuilt |

The twelve new tests in this pass are the seams: `/api/config` mints a guest and emits the
cookie; the identity survives the next request; a human survives the switch to private; the
admin router is mounted and answers 404 to everyone else (the lowercase 'f' in
`{"detail": "Not found"}` is the mechanical proof the router — not the SPA's static handler —
produced it); a superuser reaches `/api/admin/overview` through `app.main`; a cross-origin
write is refused; the kill switch flips through the real `PUT /api/admin/settings` and the
guest's *next* request no longer resolves; `/readyz` follows a model override; and the
runtime model and effort reach the agent, the record and the log.

### What this could NOT verify

Be suspicious of anything below until it has been run.

* **Real Postgres.** Every test in this suite runs against SQLite or a stub pool. The
  Postgres-only constructs in the admin API and the settings layer — `FILTER (WHERE …)`,
  `ON CONFLICT … excluded`, `NULLS LAST`, row-level `EXISTS` guards inside `UPDATE`,
  `make_interval(days => $1::int)` in `purge-guests`, CITEXT case-insensitivity, the JSONB
  round trip — have been reasoned about and executed on SQLite, not on Postgres.
* **The migration.** `0005_admin` renders correct SQL offline in both directions. It has not
  been applied to a live database (`make migrate`), and the CHECK constraint, the partial
  index and the `ON DELETE SET NULL` on `app_setting.updated_by` are unexercised.
* **The live API.** No OpenAI call was made. That switching the model in the panel changes
  what the Responses API actually runs is asserted at the agent boundary (`Agent.model`,
  `ModelSettings.reasoning.effort`), not against OpenAI.
* **The browser.** The demo pill, the ✦ link, `/backofficeadminpanel` vs `/nonsense` parity, the
  guest-session-pending screen and «Почати заново» were verified by the UI chunk in Chrome
  before this pass rewired `lib/backofficeadminpanel.ts`. The admin panel's data path (overview numbers,
  model save, user actions) has been type-checked and built, but **not** rendered against a
  running server. That is the single most valuable thing to do next.
* **Closing and reopening a real browser.** The 90-day `Max-Age` is verified on the wire; the
  browser honouring it is not.
* **Concurrency and multiple workers.** The 3-second convergence window for `model` /
  `reasoning_effort` across uvicorn workers is by design and untested; `access_mode` bypasses
  it entirely.
* **Scale.** `purge-guests`, and the 1000-row window the users table pages inside, have not
  been exercised against a table with thousands of guests in it.

### The first live pass, in order

```bash
make up && make migrate && make ingest
docker compose exec app python -m app.cli create-user anton --superuser
# sign in, open /backofficeadminpanel
#   → the overview renders, access mode reads «приватний»
#   → flip to «публічний»; in a private window, open / and check a guest session appears
#   → run one classification as the guest; check it appears in the guest's /history and in
#     the panel's «Сьогодні» tile
#   → flip back to «приватний»; the guest's next action must end their session
docker compose exec app python -m app.cli purge-guests --older-than-days 0
```
