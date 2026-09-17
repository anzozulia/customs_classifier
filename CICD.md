# CI/CD — the operator guide

Numbered steps, in order, from a repo with no remote to a VPS serving traffic. Every step
has the exact command and what a successful result looks like. Do them in order; C depends
on nothing, but F cannot work until A–E are all done.

Replace these throughout:

| placeholder | example | where it comes from |
|---|---|---|
| `<YOU>` | `antonz` | your GitHub username or org |
| `<REPO>` | `uktzed-v2` | the GitHub repository name |
| `<VPS>` | `203.0.113.10` | the VPS IP or hostname |
| `<DOMAIN>` | `uktzed.example.com` | the public hostname, already in OpenAI's domain allowlist |

---

## What the pipeline does

```
  push / PR to master or dev
            │
            ▼
  ┌─────────────────────────────────────────────────────┐
  │ ci.yml   lint · test · eval · web   (in parallel)    │
  │          image (docker build, no push — not on master)│
  └─────────────────────────────────────────────────────┘
            │ all green, AND the event was a push to master
            ▼
  ┌─────────────────────────────────────────────────────┐
  │ deploy.yml                                          │
  │   1. docker build → push ghcr.io/<YOU>/<REPO>       │
  │      tags: sha-<short7>  and  latest                │
  │   2. scp deploy.sh + docker-compose.prod.yml +      │
  │      Caddyfile to the VPS, then run deploy.sh       │
  │   3. verify /readyz — roll back and fail if not 200 │
  └─────────────────────────────────────────────────────┘
```

**CI builds, the VPS pulls.** The VPS never runs `docker build`. `npm ci` + a Vite bundle +
a full pip resolve (which includes a git clone of chatkit-python) is minutes of CPU and
enough RAM to OOM a small box halfway through a deploy. A pull of prebuilt layers takes
seconds, and tagging every build `sha-<short>` is what makes rollback one command.

**A red CI cannot deploy.** `deploy.yml` triggers on `workflow_run` and every job is behind
`github.event.workflow_run.conclusion == 'success'`.

---

## The secrets

Repository → **Settings** → **Secrets and variables** → **Actions** → **New repository
secret**. Three, and only three:

| secret | required | what it is | set in step |
|---|---|---|---|
| `VPS_HOST` | yes | the VPS IP or hostname, nothing else — no `root@`, no port | C |
| `VPS_SSH_KEY` | yes | the **private** half of the deploy key, whole file including the `-----BEGIN`/`-----END` lines and the trailing newline | C |
| `VPS_PORT` | no | the sshd port. Leave it unset unless you moved sshd; the workflow defaults to `22` | C |

Pushing to GHCR needs **no PAT**: `deploy.yml` declares `permissions: packages: write` and
logs in with the built-in `GITHUB_TOKEN`, which exists only for the life of the job. The
**VPS** is the side that needs a PAT, because it is pulling a private package and is not a
GitHub Actions runner — that is step D.

---

## Step 0 — before the first push

Run every gate locally first. It is the same work either way, and a red check on a repo
whose Actions tab has never been green is the least informative state to debug from.

**0.1 — formatting.** The `lint` job runs `ruff check` **and** `ruff format --check` —
exactly what `make lint` does:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check . | tail -1
```

Success: `All checks passed!` and `80 files already formatted`.

This was red at HEAD and **`ruff format .` has already been run** across the working tree
(20 files in `app/`, `tests/`, `evals/`, `scripts/` and `migrations/` — whitespace and quote
style, no behaviour). Those changes are uncommitted; read the diff before committing:

```bash
git diff --stat
```

If this step is ever red again, `make fmt` is the fix — it is a formatting diff, not a
defect. One caveat: `ci.yml` installs `ruff>=0.14.5`, so a new ruff release can reformat
something this repo formatted with an older one. The fix then is an exact pin in
`pyproject.toml`'s dev group, not a looser check.

**0.2 — the coverage gate.** The `test` job runs `--cov-fail-under=80`:

```bash
.venv/bin/python -m pytest -q --cov=app --cov-report=term --cov-fail-under=80
```

Success: `Required test coverage of 80% reached. Total coverage: 94.60%` — measured, with
`app/cli.py`, `app/agent/tools_nav.py`, `app/tariff/repo.py`, `app/chat/routes.py`,
`app/tariff/catalogue.py` and `app/db.py` all at 100%. That is roughly fifteen points of
headroom, so the gate is a regression alarm and not a thing you have to nurse.

One detail worth knowing before it bites: `--cov-fail-under` compares the **unrounded**
total, while the report rounds. A run that prints `80%` can still fail at 79.92%. If the
number ever drifts back down toward the gate, believe the `Total coverage:` line in the
failure message, not the `TOTAL` row.

**0.3 — the other two jobs**, so nothing is left to discover on the runner:

```bash
PATH="$PWD/.venv/bin:$PATH" make eval-verify     # the Makefile calls bare `python`
cd web && npm ci && npx tsc --noEmit && npm run build && cd ..
```

Success: `PASS — 44 code(s) exist and are terminal, 1 heading(s) exist, 55 id(s) unique.`
and a Vite build with no type errors.

---

## Step A — create the GitHub repo and push

There is no remote configured and `gh` is not installed, so this is the web UI plus plain
git.

**A.1** Open <https://github.com/new>.

- Owner: `<YOU>` · Repository name: `<REPO>`
- **Private**
- **Do not** tick "Add a README file", ".gitignore" or "Choose a license". An initialised
  repo has a commit yours does not descend from, and the first push is then rejected as a
  non-fast-forward.

Click **Create repository**. You land on a page headed "Quick setup".

**A.2** Confirm no secret is about to be committed. `.env` is in `.gitignore`; prove it:

```bash
cd /path/to/uktzed-v2
git status --porcelain
git check-ignore -v .env
```

Expect `.gitignore:1:.env	.env` from the second command, and no `.env` anywhere in the
first. `.coverage`, `coverage.xml` and `htmlcov/` are build output; they are **already**
in `.gitignore` — confirm rather than re-add:

```bash
git check-ignore -v .coverage coverage.xml
```

**A.3** Add the remote and push. Use SSH if you already have a GitHub SSH key, HTTPS
otherwise:

```bash
git remote add origin git@github.com:<YOU>/<REPO>.git     # SSH
# git remote add origin https://github.com/<YOU>/<REPO>.git   # HTTPS
git push -u origin master
```

Success: `branch 'master' set up to track 'origin/master'`, and the repo page now shows
your files. The **Actions** tab shows a `ci` run starting within a few seconds — this first
run does **not** deploy, because `deploy.yml` needs the `ci` run to finish successfully and
there is nothing on the VPS yet. If CI is red here, go back to step 0.

---

## Step B — the dev branch and branch protection on master

**B.1** Create `dev` and push it:

```bash
git switch -c dev
git push -u origin dev
git switch master
```

Success: `github.com/<YOU>/<REPO>/branches` lists both. From here on, work on `dev` and
reach `master` through a pull request — a push to `master` deploys to production.

**B.2** Protect master. **Settings** → **Branches** → **Add branch protection rule**:

- Branch name pattern: `master`
- ☑ **Require a pull request before merging** (uncheck "Require approvals" — you are one
  person)
- ☑ **Require status checks to pass before merging**
  - ☑ Require branches to be up to date before merging
  - In the search box add all four: **`ruff`**, **`pytest + coverage`**,
    **`golden set (offline)`**, **`tsc + vite build`**

> A status check only appears in that search box **after it has reported at least once**.
> If the box is empty, open one pull request from `dev` to `master` first, let CI run, then
> come back — the names will be there.

- ☑ **Do not allow bypassing the above settings** (optional; it applies the rule to you too)

Click **Create**. Success: pushing straight to master is now refused with
`protected branch hook declined`, and the dev → master PR shows four required checks.

Note that the `docker build` job is deliberately **not** a required check: it is skipped on
master pushes, and a required check that never runs blocks a merge forever.

---

## Step C — the deploy SSH key

A key used by nothing but GitHub Actions. Keep it separate from your personal key so
revoking it costs you nothing.

**C.1** Generate it, on your laptop, with no passphrase — a CI job cannot type one:

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy@<REPO>" -f ~/.ssh/uktzed_deploy -N ""
```

Success: `~/.ssh/uktzed_deploy` (private) and `~/.ssh/uktzed_deploy.pub` (public).

**C.2** Install the public half on the VPS:

```bash
ssh-copy-id -i ~/.ssh/uktzed_deploy.pub root@<VPS>
```

If that fails with `Permission denied (publickey)` — root password login is disabled, which
is common on a fresh image — append it by hand from a session you already have:

```bash
ssh root@<VPS>
mkdir -p /root/.ssh && chmod 700 /root/.ssh
cat >> /root/.ssh/authorized_keys <<'KEY'
ssh-ed25519 AAAA...the contents of uktzed_deploy.pub on one line... github-actions-deploy@uktzed-v2
KEY
chmod 600 /root/.ssh/authorized_keys
exit
```

**C.3** Prove the key works before handing it to CI:

```bash
ssh -i ~/.ssh/uktzed_deploy -o BatchMode=yes root@<VPS> 'whoami && uname -a'
```

Success: `root`, then the kernel line, with no password prompt. `BatchMode=yes` is what
makes it fail instead of prompting — exactly what the runner will experience.

**C.4** Add the secrets. **Settings** → **Secrets and variables** → **Actions**:

```bash
pbcopy < ~/.ssh/uktzed_deploy        # macOS. Linux: xclip -sel clip < ~/.ssh/uktzed_deploy
```

- `VPS_SSH_KEY` → paste. It must start `-----BEGIN OPENSSH PRIVATE KEY-----` and end
  `-----END OPENSSH PRIVATE KEY-----` with a newline after it. A key that lost its trailing
  newline fails at `Load key: invalid format`.
- `VPS_HOST` → `<VPS>`. Just the host. Not `root@<VPS>`.
- `VPS_PORT` → only if sshd is not on 22.

Success: the Actions secrets page lists `VPS_HOST` and `VPS_SSH_KEY`. You cannot read them
back, which is the point.

---

## Step D — a GHCR read token, and docker login on the VPS

The image is a private package. The VPS is not a runner, so it cannot use `GITHUB_TOKEN`
and needs a token of its own — read-only, so a compromised VPS cannot publish an image.

**D.1** <https://github.com/settings/tokens> → **Tokens (classic)** → **Generate new token
(classic)**.

- Note: `uktzed-vps-ghcr-read`
- Expiration: 1 year (calendar it — an expired token makes the *next* deploy fail at
  `docker pull`, with `denied`, and deploy.sh says exactly that)
- Scopes: **`read:packages`** only. Nothing else. Not `repo`, not `write:packages`.

Click **Generate token** and copy it. Classic, not fine-grained: fine-grained tokens do not
grant container-registry read on another account's package cleanly.

**D.2** Log in on the VPS:

```bash
ssh root@<VPS>
echo 'ghp_xxxxxxxxxxxxxxxxxxxx' | docker login ghcr.io -u <YOU> --password-stdin
```

Success: `Login Succeeded`. Credentials land in `/root/.docker/config.json` — base64, not
encrypted, so:

```bash
chmod 600 /root/.docker/config.json
```

**D.3** You cannot test the pull until the first image exists (step F). If step F fails at
`could not pull`, come back here.

---

## Step E — prepare the VPS

Everything lives in `/root/uktzed-v2` and runs as root. SSH access to this box is the trust
boundary; anyone who has it has `docker inspect` too.

**E.1** Docker:

```bash
ssh root@<VPS>
curl -fsSL https://get.docker.com | sh
docker --version && docker compose version
```

Success: Docker `27.x` or newer, and `Docker Compose version v2.x`. The compose **plugin**
(`docker compose`, no hyphen) is required; `docker-compose` v1 cannot read this file.

**E.2** The directory. This and the `.env` below are the **only** two things you place by
hand — `deploy.sh`, `docker-compose.prod.yml` and the `Caddyfile` are copied in by every
deploy:

```bash
mkdir -p /root/uktzed-v2
cd /root/uktzed-v2
```

**E.3** The firewall, if one is enabled:

```bash
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw status
```

Success: 22, 80 and 443 `ALLOW`. Port 443/udp too if you want HTTP/3. Postgres and the app
publish no ports at all, so there is nothing else to open.

**E.4** DNS, **before** the first deploy. Caddy asks Let's Encrypt for a certificate the
moment it starts, and a failed challenge burns rate limit:

```bash
dig +short <DOMAIN>
```

Success: the VPS IP. Also confirm `<DOMAIN>` is already registered at
platform.openai.com → organization → security → domain allowlist. Propagation there is
minutes to ~30 minutes, and if the domain key is wrong ChatKit fails closed and the chat
box silently does not appear.

**E.5** The `.env`. Generate a session secret first:

```bash
openssl rand -base64 48
```

Then write the file, substituting the real values:

```bash
cat > /root/uktzed-v2/.env <<'EOF'
# ---- OpenAI ----
OPENAI_API_KEY=sk-proj-...
OPENAI_ORG_ID=
MODEL=gpt-5.6-terra
REASONING_EFFORT=low
OPENAI_TELEMETRY=true

# ---- ChatKit ----
# MUST be the real domain_pk_... for <DOMAIN>. The app REFUSES TO BOOT with the
# placeholder when PUBLIC_BASE_URL is https://, and that refusal is deliberate: the
# placeholder makes ChatKit unmount silently, which looks like a frontend bug for a day.
CHATKIT_DOMAIN_KEY=domain_pk_...

# ---- App ----
DATABASE_URL=postgresql://postgres:CHANGE_ME@db:5432/uktzed
SESSION_SECRET=<paste the openssl output>
LOG_LEVEL=info

# ---- Caddy / TLS ----
UKTZED_DOMAIN=<DOMAIN>
# Exactly https://<DOMAIN>. Lowercase, no :443, no trailing slash. Every POST's Origin
# header is compared to this string, so a mismatch means every message, login and admin
# action is 403 while GETs keep working. It also decides is_production.
PUBLIC_BASE_URL=https://<DOMAIN>
ACME_EMAIL=you@example.com

# ---- Postgres ----
POSTGRES_DB=uktzed
POSTGRES_USER=postgres
POSTGRES_PASSWORD=CHANGE_ME
EOF
chmod 600 /root/uktzed-v2/.env
```

`POSTGRES_PASSWORD` and the password inside `DATABASE_URL` must be the same string. The
database publishes no port, so this password is only reachable from inside the compose
network — but set it to something random anyway:

```bash
openssl rand -hex 24
```

Success:

```bash
ls -l /root/uktzed-v2/.env          # -rw------- 1 root root
grep -c . /root/uktzed-v2/.env      # a count, and no error
```

---

## Step F — the first deploy, then the two one-time data steps

**F.1** Trigger a deploy by landing a commit on master. With branch protection on, that
means a pull request:

```bash
git switch dev && git push
```

Open `github.com/<YOU>/<REPO>/compare/master...dev`, **Create pull request**, wait for the
four checks, **Merge pull request**.

Watch **Actions**. Expect `ci` to go green, then a `deploy` run to start on its own.

Success looks like, in the deploy job log:

```
Building ghcr.io/<you>/<repo>:sha-1a2b3c4
...
=== deploy ghcr.io/<you>/<repo>:sha-1a2b3c4 (replacing <nothing>) ===
waiting up to 180s for /healthz
/healthz OK: HTTP 200 {"status":"ok"}
the app is LIVE but not READY: the tariff has never been ingested on this box.
=== deployed ghcr.io/<you>/<repo>:sha-1a2b3c4 (rollback target: none) ===
```

That `LIVE but not READY` is correct and expected. `/readyz` checks for an ingested tariff
and there is not one yet, so the first deploy is gated on liveness instead. The gate becomes
`/readyz` — with automatic rollback — as soon as F.2 succeeds.

**F.2** Ingest the tariff. **One time**, and again only when
`data/uktzed_hierarchical.json` changes. It is never part of a deploy: it is a data
operation, and a deploy that also rewrites 14,187 rows cannot be undone by re-pointing an
image tag.

```bash
ssh root@<VPS> '/root/uktzed-v2/deploy.sh ingest'
```

Success: `14187` nodes / `10490` terminals, then

```
bootstrapped. From now on a deploy that does not reach /readyz 200 is rolled back.
HTTP 200 {"status":"ok","checks":{"db":true,"dataset_sha256":"...","tariff_nodes":14187}}
```

**F.3** Create the first superuser. **One time.** There is no registration route anywhere in
the application, and the admin panel is the only way to make the deployment public — so this
chain has to start from a shell:

```bash
ssh -t root@<VPS> '/root/uktzed-v2/deploy.sh compose exec app python -m app.cli create-user anton --superuser'
```

Success: a generated password, printed **once**. Copy it now; it is not recoverable and
`set-password` is how you replace it.

**F.4** Confirm the whole path from the outside — not from the VPS:

```bash
curl -s https://<DOMAIN>/readyz | python3 -m json.tool
```

Success: `"status": "ok"`, `"tariff_nodes": 14187`, and a `model`. Then open
`https://<DOMAIN>/` in a browser with the DevTools console open and check the ChatKit
list in `README.md`'s pre-deploy checklist — no "Domain verification failed", no
"Domain verification skipped", no CSP violations.

---

## Step G — day two

Everything below runs on the VPS. `deploy.sh compose` is the passthrough: it sources
`deploy.state` first, which the compose file requires, so use it instead of a bare
`docker compose`.

### Roll back

```bash
ssh root@<VPS> '/root/uktzed-v2/deploy.sh status'      # what is live, what rollback goes to
ssh root@<VPS> '/root/uktzed-v2/deploy.sh rollback'
```

A failed deploy has already rolled itself back — `rollback` is for the case where the deploy
went green and the problem showed up afterwards. It is idempotent: running it twice
re-applies the same known-good tag rather than flip-flopping.

It does **not** downgrade migrations. Re-pointing an image tag is reversible; dropping a
column is not. If a migration is what broke the old version, that is a deliberate manual
`alembic downgrade <rev>`.

To go back further than one step, deploy a specific tag by hand:

```bash
ssh root@<VPS> '/root/uktzed-v2/deploy.sh deploy ghcr.io/<YOU>/<REPO>:sha-1a2b3c4'
```

Tags are listed at `github.com/<YOU>/<REPO>/pkgs/container/<REPO>`.

### Read logs

```bash
ssh root@<VPS> '/root/uktzed-v2/deploy.sh compose logs -f --tail=100 app'
ssh root@<VPS> '/root/uktzed-v2/deploy.sh compose logs --tail=50 migrate'
ssh root@<VPS> '/root/uktzed-v2/deploy.sh compose logs --tail=50 caddy'
cat /root/uktzed-v2/deploy.log                  # every deploy this box has ever run
```

Container logs are capped at 10 MB × 5 per container, so they cannot fill the disk and stop
Postgres accepting writes. The deploy log is not capped; it is a few lines per deploy.

### Flip the access mode

`access_mode` is `private` (logins only) or `public` (anonymous guests). It defaults to
`private` and fails closed — an unreachable database resolves to `private`, never to
`public`.

Normal path: sign in as the superuser at `https://<DOMAIN>/backofficeadminpanel` and use
the toggle. It is read from the primary on every request with no cache, so it takes effect
on the next request, not in three seconds.

Break-glass, when you cannot reach the panel — note the value is JSONB, so it is a quoted
JSON string:

```bash
ssh root@<VPS> $'/root/uktzed-v2/deploy.sh compose exec -T db psql -U postgres -d uktzed -c "INSERT INTO app_setting (key, value, updated_at, updated_by) VALUES (\'access_mode\', \'\\"private\\"\'::jsonb, now(), NULL) ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now(), updated_by = NULL;"'
```

Success: `INSERT 0 1`. Check it:

```bash
ssh root@<VPS> '/root/uktzed-v2/deploy.sh compose exec -T db psql -U postgres -d uktzed -c "SELECT key, value, updated_at FROM app_setting;"'
```

### Re-ingest after a tariff update

```bash
ssh root@<VPS> '/root/uktzed-v2/deploy.sh ingest'
```

Idempotent per content hash: unchanged file, no writes, same counts. Deploy the image
containing the new JSON first — the file is baked into the image at `/srv/data/`.

### Reclaim disk

```bash
ssh root@<VPS> 'df -h / && /root/uktzed-v2/deploy.sh prune'
```

Deletes every old `sha-` tag, keeping the live one and the rollback target. Never run it
during an incident: it is exactly what would delete the image you are about to roll back to.

### Rotate the GHCR token

When `read:packages` expires, deploys fail at `could not pull ... denied`. Redo step D.1 and
D.2; nothing else changes.

### What to back up

`README.md` has the commands. Two things: the Postgres dump (every classification and every
user) and the `caddy_data` volume (the ACME account key and your certificates — Let's
Encrypt rate-limits duplicate certificates to 5 per registered domain per week, so losing it
during a redeploy loop can cost you TLS for days).

Note what `deploy.sh` will never do: it never runs `docker compose down`, and it never runs
`down -v`. That flag destroys `pg_data`.

---

## When something goes wrong

| symptom | where | what it means |
|---|---|---|
| `Load key "/home/runner/.ssh/id_deploy": invalid format` | deploy, "install the deploy key" | `VPS_SSH_KEY` lost its trailing newline, or you pasted the `.pub` |
| `Permission denied (publickey)` | deploy, "sync deploy files" | the public half is not in `/root/.ssh/authorized_keys` — redo C.2 |
| `could not pull ... denied` | deploy.sh | the VPS GHCR token expired or was never created — redo step D |
| `missing /root/uktzed-v2/.env` | deploy.sh preflight | step E.5 was skipped |
| `the migration failed. NOTHING was deployed` | deploy.sh | the old version is still serving. Read the alembic traceback above it |
| `failed readiness and was rolled back` | deploy.sh | the previous tag is live again. `deploy.sh compose logs --tail=200 app` |
| `the rollback to ... ALSO failed` | deploy.sh | the site is down. Get on the box |
| `the Caddyfile was rejected` | deploy.sh | the site is **up** on the previous proxy config; your Caddyfile change is not live |
| deploy never starts after a green CI | Actions | the merge produced no `push` event on `master`; or `ci.yml`'s `name:` was changed and no longer matches `workflow_run: workflows: ["ci"]` in `deploy.yml`; or `deploy.yml` is not on the repository's **default branch** — `workflow_run` only fires for workflow files that exist there, so if the default branch is not `master`, either change it in Settings → General or merge `deploy.yml` into whatever it is |
| `variable is not set` from compose | a bare `docker compose` on the VPS | use `./deploy.sh compose ...`; the compose file requires `APP_IMAGE`/`APP_TAG` from `deploy.state` |
