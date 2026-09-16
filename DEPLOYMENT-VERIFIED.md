# Deployment path — verified 2026-09-16

The full `docker compose` stack was built and exercised end to end on this machine.
Everything below is measured output, not intent.

## Stack

```
SERVICE   STATUS
app       Up (healthy)      uktzed-app:dev, 495MB, runs as non-root `app`
caddy     Up                0.0.0.0:80->80, 0.0.0.0:443->443 (tcp+udp)
db        Up (healthy)      postgres:17
migrate   Exited 0          ran BEFORE app started
```

`app` and `db` publish **no host ports**. Only Caddy is reachable from outside.
(v1 published 5432 with the default password.)

## Boot order works

`migrate` runs `alembic upgrade head` to completion and exits 0 before `app` starts,
via `depends_on: service_completed_successfully`:

```
0001_users -> 0002_chatkit_store -> 0003_tariff -> 0004_records
```

## Readiness is a real check, not a rubber stamp

On a fresh stack, before ingest:

```
GET /readyz -> 503
{"status":"not_ready","checks":{"db":true,"tariff_nodes":"error: NoActiveDatasetError",...}}
```

After `make ingest` (`python -m app.cli ingest data/uktzed_hierarchical.json`):

```
GET /readyz -> 200
{"status":"ok","checks":{"db":true,"dataset_sha256":"5a113fc09ac0",
                         "tariff_nodes":14187,"model":"gpt-5.6-terra"}}
```

The model id is read from config. v1 logged a banner claiming `o3` + `gpt-5` while running
`o4-mini` + `gpt-4.1`, for two months.

## TLS and routing

`http://localhost/*` -> 308 -> `https://localhost/*`. The SPA is served at `/`
(`<title>Класифікатор УКТЗЕД</title>`) and references
`https://cdn.platform.openai.com/deployments/chatkit/chatkit.js`, which is confirmed
reachable (200, 27,929 bytes).

## SSE is NOT buffered by the proxy — the thing most likely to break

A real classification streamed through Caddy:

```
  +  0.06s  event   1  thread.created
  +  0.06s  event   4  progress_update
  +  3.01s  event   5  thread.item.added
  +  5.67s  event  90  thread.item.updated
--- first event at 0.06s · 96 events · stream ended 14.32s ---
VERDICT: STREAMED INCREMENTALLY (proxy is not buffering)
```

First byte in 60 ms against a 14.3 s turn. v1's equivalent was a static
"🔍 Обробляю ваш запит..." string in front of a 48 s median cold wait.

## DEPLOY-DAY TRAP found here, fix before you ship

`PUBLIC_BASE_URL` must **exactly match the public origin**, scheme included. The stack came up
with the default `http://localhost:8000` while being served at `https://localhost`, and every
login returned:

```
POST /api/login -> 403 {"detail":"Cross-origin request rejected"}
```

That is `require_same_origin` doing its job, and it is silent about the cause. On a real
deploy set `PUBLIC_BASE_URL=https://your.domain` and `UKTZED_DOMAIN=your.domain` together.

## Still not verified

* **The domain-key gate.** ChatKit verifies your domain against the OpenAI org allowlist and
  fails closed by *unmounting the chat*. It is skipped on localhost/non-443, so it cannot be
  tested here — only on the first real HTTPS deploy. Register the hostname days ahead;
  propagation is reported at minutes to ~30 minutes.
* **Visual rendering** of ProgressUpdateEvent, the Thinking panel and widgets. The events are
  demonstrably emitted; how they look has not been seen.
* `scripts/` is not copied into the image. `make ingest` uses `python -m app.cli ingest`,
  which is the supported path; `python scripts/ingest.py` works only outside the container.
