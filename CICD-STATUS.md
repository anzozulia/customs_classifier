# CI/CD — status

What exists, what it was measured at, and what is **still on you** before the first deploy
can work. The step-by-step operator guide is [`CICD.md`](CICD.md); this file is the summary
and the honest gap list.

Everything below was verified on this machine on the working tree as it stands. Nothing is
committed — `git status` still shows the whole change set.

---

## Where it stands

| | |
|---|---|
| test suite | **565 passed, 1 xfailed**, 3.7 s (5.4 s with coverage) |
| coverage of `app/` | **94.6%** — 2445 statements, 132 missed, gate is 80% |
| `ruff check .` | clean |
| `ruff format --check .` | clean — 80 files (it was **red at HEAD**; `ruff format .` has been run, 20 files) |
| workflow YAML | both files parse; `dependabot.yml` parses |
| `bash -n deploy/deploy.sh` | clean, and the file is `0755` |
| prod vs dev compose | identical after stripping `image:`/`build:` |
| `make eval-verify` | `PASS — 44 code(s) exist and are terminal, 1 heading(s) exist, 55 id(s) unique.` |
| SPA | `tsc --noEmit` clean, `vite build` clean (318 kB / 98 kB gzip) |
| no-`.env` run | the whole suite passes with **no `.env` and a scrubbed environment** — i.e. the way a runner sees it |

The six modules the coverage work targeted are all at 100.0%: `app/cli.py` (0% → 100%),
`app/agent/tools_nav.py` (38% → 100%), `app/tariff/repo.py` (55% → 100%),
`app/chat/routes.py` (62% → 100%), `app/tariff/catalogue.py` (68% → 100%),
`app/db.py` (70% → 100%).

Reproduce the number:

```bash
.venv/bin/python -m pytest -q --cov=app --cov-report=term-missing --cov-fail-under=80
# Required test coverage of 80% reached. Total coverage: 94.60%
```

Largest remaining gaps, if the number ever needs raising again: `app/tariff/ingest.py` 81.6%
(34 missed — the `main()` CLI half), `app/agent/tools_terminal.py` 80.2% (41 — the widget
rendering), `app/chat/errors.py` 80.4% (11 — per-provider error branches),
`app/main.py` 88.3% (12 — lifespan failure paths).

---

## What CI checks

`.github/workflows/ci.yml`, named **`ci`** — the name is load-bearing, `deploy.yml` matches
on it. Triggers on push **and** pull request to `master` and `dev`.

| job | name in the checks list | what it runs |
|---|---|---|
| `lint` | `ruff` | `ruff check .` then `ruff format --check .` (identical to `make lint`) |
| `test` | `pytest + coverage` | `pytest -q --cov=app --cov-report=term-missing --cov-report=xml --cov-fail-under=80`, uploads `coverage.xml` even on failure |
| `eval` | `golden set (offline)` | `make eval-verify` — every `expect_code` in `evals/golden/*.yaml` looked up in the committed tariff. No API key, no database |
| `web` | `tsc + vite build` | `npm ci`, `npx tsc --noEmit`, `npm run build` |
| `image` | `docker build` | builds the Dockerfile, **never pushes**; skipped on pushes to master (deploy builds the same image). Waits on lint+test+web |

The suite needs **no services**: the SQL runs against SQLite through a fake pool, HTTP
through `TestClient` over a stub pool. That is why there are no service containers.

Two facts worth keeping in mind:

- **`pytest-cov` is not declared** in `pyproject.toml` (neither `dependencies` nor the `dev`
  group), so the test job installs `"pytest-cov>=7,<8"` explicitly. If you ever add it to the
  dev group, drop it from `ci.yml`.
- **`--cov-fail-under` compares the unrounded total.** A report that prints `80%` can still
  fail at 79.92%; `precision = 1` in `[tool.coverage.report]` is set so that is visible.

## What deploy does

`.github/workflows/deploy.yml` — master only, and it cannot run unless CI went green.

1. **Gate.** `workflow_run` on the `ci` workflow, with
   `conclusion == 'success' && event == 'push'`, filtered to `branches: [master]`. The
   `event == 'push'` half matters: `workflow_run` also fires for PR-triggered CI runs. The
   `deploy` job `needs: publish`, so when the gate is false both jobs skip.
2. **Build and push** `ghcr.io/<owner>/<repo>:sha-<short7>` (plus `:latest`, for humans —
   deploy.sh never uses it, because `:latest` cannot be rolled back to). Everything checks
   out `github.event.workflow_run.head_sha`, never `github.sha`, which in a `workflow_run`
   event is the default-branch tip and not the tested commit.
3. **Copy three files** to the VPS on every deploy — `deploy.sh`,
   `docker-compose.prod.yml`, `Caddyfile`. The box has no checkout, so otherwise changes to
   them would never ship.
4. **`deploy.sh deploy <image>:<tag>`** on the box: pull → start db → `alembic upgrade head`
   in a one-shot container **while the old app is still serving** → record the rollback
   target → `compose up -d` → wait for `/readyz` (up to 180 s) → `caddy reload`.
5. **`deploy.sh verify`** as an independent second look, including that Caddy answers on
   :80.
6. **Automatic rollback** if either of those fails: re-point to `PREVIOUS_TAG`, bring it up,
   wait for ready. Migrations are deliberately **not** downgraded.

**First deploy is special.** `/readyz` returns 503 until the tariff is ingested, so until
`/root/uktzed-v2/.bootstrapped` exists the gate is `/healthz` (liveness) and `verify` is
soft. `./deploy.sh ingest` creates that flag on success; from then on every deploy gates
hard on `/readyz` with rollback.

**Ingest is never automatic.** It is a data operation, and a deploy that rewrites 14,187
rows cannot be undone by re-pointing an image tag.

---

## What you must do by hand before the first deploy works

In this order. `CICD.md` has the exact commands and what success looks like for each.

1. **Commit and push.** Nothing here is committed. The working tree carries the four new
   test files, the workflows, `deploy/`, the docs, the `[tool.coverage]` block, the
   `.gitignore` addition and the `ruff format` diff. — `CICD.md` step 0 / A
2. **Create the GitHub repo, private, un-initialised**, add the remote, push `master`.
   `deploy.yml` must end up on the repository's **default branch** or `workflow_run` never
   fires. — step A
3. **Create `dev`** and protect `master`: require a PR, and require the four checks `ruff`,
   `pytest + coverage`, `golden set (offline)`, `tsc + vite build`. A check only appears in
   that picker after it has reported once. Do **not** require `docker build` — it is skipped
   on master and a required check that never runs blocks the merge forever. — step B
4. **Three repository secrets**, and only three — `VPS_HOST`, `VPS_SSH_KEY` (private half,
   whole file, trailing newline included), `VPS_PORT` (optional, defaults to 22). No secret
   in either workflow is undocumented, and nothing documented is unused. GHCR **push** needs
   no PAT. — step C
5. **A GHCR read token on the VPS**: a *classic* PAT with `read:packages` only, then
   `docker login ghcr.io` on the box. This is the one credential the VPS needs and the one
   that will expire on you in a year — calendar it. — step D
6. **Prepare the VPS**: Docker + the compose **v2 plugin**, `mkdir -p /root/uktzed-v2`,
   firewall 22/80/443, and DNS for the domain pointing at the box **before** the first
   deploy (Caddy asks Let's Encrypt the moment it starts, and a failed challenge burns rate
   limit). — step E
7. **Write `/root/uktzed-v2/.env` by hand**, `chmod 600`. It is the only thing besides the
   directory that you place manually; it is never copied by CI and never in git. The real
   `CHATKIT_DOMAIN_KEY` and `PUBLIC_BASE_URL=https://<domain>` are both load-bearing — the
   app refuses to boot with the placeholder key on an https base URL, on purpose. — step E.5
8. **After the first deploy**: `./deploy.sh ingest` (14187 nodes / 10490 terminals — this is
   what arms the hard `/readyz` gate), then
   `./deploy.sh compose exec app python -m app.cli create-user <name> --superuser`. There is
   no registration route; that shell command is the only way a first login exists. — step F

Optional, later: `<DOMAIN>` must be in the OpenAI org domain allowlist or ChatKit fails
closed and the chat box silently does not render.

---

## Known gaps and judgement calls

- **A real defect is pinned, not fixed.** `list_groups_in_section` in
  `app/agent/tools_nav.py` builds its breadcrumb with `_breadcrumb(repo, code)`, and
  `TariffRepo.ancestry` derives the level from the code's **length** — so a section code is
  looked up as a group. `list_groups_in_section("16")` returns the right groups under
  section 16's number but the breadcrumb of *group* 16. A section has no ancestors; the fix
  is `path=[]` (or the section's own row) in the tool. It is recorded as
  `tests/test_tools_nav.py::test_the_section_listing_does_not_borrow_the_groups_breadcrumb`,
  `@pytest.mark.xfail(strict=True)`: green today, and it turns into a **failing** test the
  moment somebody fixes the code without removing the marker. That is intended — do not
  delete the marker without making the fix.
- **`ruff>=0.14.5` floats in CI.** A ruff release can reformat something this tree formatted
  with 0.16.7 and turn `lint` red with no code change. The fix is an exact pin in the dev
  group, not a weaker check.
- **`ruff format` rewrote code blocks in `evals/README.md`.** ruff formats Markdown code
  blocks now; that file's examples changed shape, not content.
- **Host-key trust-on-first-use.** `deploy.yml` learns the VPS host key with `ssh-keyscan`
  on every run. Pinning it would be stronger but needs a fourth secret.
- **Dependabot cannot see `openai-chatkit`** — it is a direct git URL pinned to a tag. Watch
  that repository's tags by hand.
- **Never exercised for real, and cannot be from here:** the GHCR push, the SSH hop, the
  `docker pull` on the box, the Let's Encrypt challenge, and the rollback path. The compose
  files, the script's control flow and the workflow graph are verified; the credentials and
  the network are not.
