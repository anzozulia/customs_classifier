# UKTZED Classifier

An AI agent that finds the 10-digit Ukrainian customs tariff code (УКТЗЕД) for a product
from its text description or a link to it in a web shop, and shows why it chose that code.

**Live demo:** https://classifier.anzozulia.com

## What it does

- **Walks the tariff tree** section → group → heading → code, and streams each step live.
- **Explains its answer:** the code, a plain-language label, the reasoning, the codes it
  rejected and why, and the official tariff text.
- **Cannot invent a code.** A code is accepted only if this turn's tool calls opened its
  branch of the tariff; anything else is sent back to the model before you see it.
- **Asks one clarifying question**, with one-click options, when the description is not
  enough to choose between codes.
- **Searches the web** for unfamiliar trade names and product links.
- **Keeps a history** of every classification: searchable, exportable to CSV, with the log
  of the agent's calls to the tariff.
- **Runs as a public demo or a private app.** A superuser can switch between the two at
  runtime from the admin panel; there is no self-registration.

## Stack

| Layer | |
|---|---|
| Agent | OpenAI Agents SDK, Responses API, Web Search Tool |
| Backend | Python 3.13, FastAPI, PostgreSQL 17 (`pg_trgm`), Alembic |
| Frontend | OpenAI ChatKit (self-hosted server mode), React 19, Vite, Tailwind CSS |
| Ops | Docker Compose, GitHub Actions, GHCR, nginx |

## Run locally

```bash
cp .env.example .env              # set OPENAI_API_KEY and SESSION_SECRET (openssl rand -base64 48)
make up                           # build and start the stack; migrations run first
make ingest                       # load the tariff: 14,187 nodes / 10,490 final codes
make superuser USERNAME=you       # prints a generated password once
```

Open `https://localhost`. The app starts **private**: sign in as the superuser and switch the
access mode to public in the admin panel to let visitors in without a login.

For development, `make dev` runs the API with reload plus the Vite dev server. `make test`,
`make lint` and `make eval-verify` (offline golden-set check) are what CI runs. `make help`
lists everything.

## Deploy

A push to `master` runs CI (ruff, pytest with an 80% coverage floor, the golden-set check,
`tsc` + `vite build`). When CI is green, the deploy workflow builds the image, pushes it to
GHCR and deploys it to the VPS over SSH. The deploy migrates the database, waits for
`/readyz`, and rolls back to the previous image if the new one never becomes ready.

It needs the GitHub secrets `VPS_HOST`, `VPS_PORT` and `VPS_SSH_KEY`, and a `.env` on the
server. Secrets live only in that file and are never synced by CI.

One-time server setup: Docker with the compose plugin, `docker login ghcr.io` with a
read-only (`read:packages`) token, the nginx vhost from `deploy/nginx/` (run `nginx -t` before
reloading), then `deploy.sh ingest` once after the first deploy to load the tariff.

## Good to know

- **The chat UI is loaded from OpenAI's CDN** and runs in a cross-origin iframe. In
  production the domain must be on your organisation's ChatKit domain allowlist, with its
  key in `CHATKIT_DOMAIN_KEY`. If verification fails, the chat removes itself from the page.
- **The classification is for reference only.** The final decision belongs to customs.

## License

MIT
