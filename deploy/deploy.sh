#!/usr/bin/env bash
#
# UKTZED v2 — the deploy script that runs ON THE VPS, as root, out of /root/uktzed-v2.
#
# It is copied here by .github/workflows/deploy.yml on every deploy, together with
# docker-compose.prod.yml and the Caddyfile. Edit it in the repo, not on the box — a local
# edit is overwritten by the next deploy and leaves no trace of why.
#
# ---------------------------------------------------------------------------------------
# INGEST IS NOT PART OF A DEPLOY, deliberately.
# ---------------------------------------------------------------------------------------
# `app.cli ingest` is idempotent per content hash — re-running it on an unchanged file is a
# no-op — so running it every deploy would be *safe*. It is still not done, because it is a
# DATA operation and a deploy is a CODE operation, and the two want different blast radii: a
# deploy that also rewrites 14,187 tariff rows cannot be undone by re-pointing an image tag.
# Run `./deploy.sh ingest` when data/uktzed_hierarchical.json changed, and only then.
#
# ---------------------------------------------------------------------------------------
# THE STATE FILE is what makes rollback possible.
# ---------------------------------------------------------------------------------------
# /root/uktzed-v2/deploy.state holds APP_IMAGE, APP_TAG and PREVIOUS_TAG. It is sourced
# before every `docker compose` call because docker-compose.prod.yml interpolates
# ${APP_IMAGE} and ${APP_TAG} with `:?`, so compose refuses to run at all without them.
# PREVIOUS_TAG is written BEFORE the new image is brought up — if it were written after,
# the one situation that needs it (the new image never comes up) is the one where it would
# not have been written.
set -euo pipefail

readonly ROOT_DIR="/root/uktzed-v2"
readonly COMPOSE_FILE="${ROOT_DIR}/docker-compose.prod.yml"
readonly STATE_FILE="${ROOT_DIR}/deploy.state"
readonly ENV_FILE="${ROOT_DIR}/.env"
readonly LOCK_FILE="${ROOT_DIR}/.deploy.lock"
readonly LOG_FILE="${ROOT_DIR}/deploy.log"
readonly TARIFF_DEFAULT="data/uktzed_hierarchical.json"

# ---------------------------------------------------------------------------------------
# THE BOOTSTRAP FLAG — why the first deploy is not gated on /readyz.
# ---------------------------------------------------------------------------------------
# /readyz answers 503 until the tariff has been ingested: it checks for an ACTIVE dataset
# with nodes in it, and a fresh Postgres volume has none. So on a brand-new box the first
# deploy is healthy, correct, and 503 — and gating it on readiness would time out, attempt
# a rollback, find no previous tag, and fail with a message about rolling back that has
# nothing to do with what actually happened.
#
# Until this file exists the gate is /healthz (liveness: the process is up and serving).
# `./deploy.sh ingest` creates it on success, and from that moment every deploy and every
# verify gates hard on /readyz. `rm .bootstrapped` re-arms the softer gate — which is what
# you want after destroying the pg_data volume, and never otherwise.
readonly BOOTSTRAP_FLAG="${ROOT_DIR}/.bootstrapped"

# How long to wait for /readyz after `up`. The app loads a 2.4 MB tariff tree at startup
# behind a 20s healthcheck start_period, and the OLD app container gets its full 90s
# stop_grace_period before the new one binds — so 180s is a real bound, not a guess.
READY_TIMEOUT="${READY_TIMEOUT:-180}"
READY_INTERVAL="${READY_INTERVAL:-5}"

APP_IMAGE=""
APP_TAG=""
PREVIOUS_TAG=""

# ------------------------------------------------------------------------------- output
# Everything goes to the console AND to deploy.log. The append to the log file is best
# effort and can never fail the script: a full disk is a thing that happens during a
# deploy, and losing the message that says so is the worst possible time to lose it.

_emit() {
  local line="$1" fd="$2"
  printf '%s  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$line" >&"$fd"
  printf '%s  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$line" >>"$LOG_FILE" 2>/dev/null || true
  return 0
}

log() { _emit "$*" 1; }
warn() { _emit "WARNING: $*" 2; }

# Loud on purpose, and untimestamped so the banner reads as a banner. This text is what an
# operator reads at 02:00 out of a CI log.
die() {
  local line
  for line in "" \
    "================================================================================" \
    "  DEPLOY FAILED: $*" \
    "================================================================================" \
    ""; do
    printf '%s\n' "$line" >&2
    printf '%s\n' "$line" >>"$LOG_FILE" 2>/dev/null || true
  done
  exit 1
}

# --------------------------------------------------------------------------- state file

load_state() {
  if [[ -f "$STATE_FILE" ]]; then
    # shellcheck source=/dev/null
    source "$STATE_FILE"
  fi
  : "${APP_IMAGE:=}" "${APP_TAG:=}" "${PREVIOUS_TAG:=}"
}

# Written through a temp file and mv, which is atomic on the same filesystem. A deploy
# interrupted mid-write must not leave a half-line that `source` then chokes on — that
# would break every subsequent invocation, rollback first among them.
save_state() {
  local tmp
  tmp="$(mktemp "${STATE_FILE}.XXXXXX")"
  {
    echo "# Written by deploy.sh. APP_TAG is live; PREVIOUS_TAG is what rollback returns to."
    echo "# Last written: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'APP_IMAGE=%q\n' "$APP_IMAGE"
    printf 'APP_TAG=%q\n' "$APP_TAG"
    printf 'PREVIOUS_TAG=%q\n' "$PREVIOUS_TAG"
  } >"$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$STATE_FILE"
}

# ------------------------------------------------------------------------------- docker

# Every compose call goes through here. --project-directory pins where `.env` and the
# ./Caddyfile bind mount resolve from, so the script behaves identically whatever directory
# it was invoked from.
compose() {
  APP_IMAGE="$APP_IMAGE" APP_TAG="$APP_TAG" \
    docker compose --project-directory "$ROOT_DIR" -f "$COMPOSE_FILE" "$@"
}

preflight() {
  [[ "${EUID}" -eq 0 ]] || die "run this as root; the whole deployment lives in ${ROOT_DIR}"
  [[ -f "$COMPOSE_FILE" ]] || die "missing ${COMPOSE_FILE} — the deploy workflow copies it there; see CICD.md step E"
  [[ -f "$ENV_FILE" ]] || die "missing ${ENV_FILE} — copy .env.example and fill it in; see CICD.md step E"
  [[ -f "${ROOT_DIR}/Caddyfile" ]] || die "missing ${ROOT_DIR}/Caddyfile — the deploy workflow copies it there; see CICD.md step E"
  command -v docker >/dev/null 2>&1 || die "docker is not installed; see CICD.md step E"
  docker compose version >/dev/null 2>&1 || die "the docker compose v2 plugin is not installed; see CICD.md step E"
}

# One deploy at a time. Two overlapping `compose up` runs on one box recreate the same
# containers from two different images and the loser wins at random.
take_lock() {
  exec 9>"$LOCK_FILE"
  flock -n 9 || die "another deploy is already running (lock: ${LOCK_FILE})"
}

# ------------------------------------------------------------------------------- probes

is_bootstrapped() { [[ -f "$BOOTSTRAP_FLAG" ]]; }

# An HTTP probe from INSIDE the app container, against 127.0.0.1:8000.
#   /healthz  liveness only — the process is up. No database, no OpenAI.
#   /readyz   readiness — database reachable + tariff ingested + an effective model.
# Both answer 200 or 503; neither redirects and neither is behind auth.
# Deliberately python and not curl: the runtime image has python and does not have curl.
http_probe() {
  compose exec -T app python - "$1" <<'PY' 2>&1
import sys, urllib.error, urllib.request

path = sys.argv[1]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:8000{path}", timeout=5) as resp:
        status, body = resp.status, resp.read().decode("utf-8", "replace")
except urllib.error.HTTPError as exc:          # a 503 from readyz arrives here
    status, body = exc.code, exc.read().decode("utf-8", "replace")
except Exception as exc:                       # nothing listening yet
    print(f"unreachable: {type(exc).__name__}: {exc}")
    sys.exit(2)

print(f"HTTP {status} {body}")
sys.exit(0 if status == 200 else 1)
PY
}

readyz_probe() { http_probe /readyz; }
healthz_probe() { http_probe /healthz; }

# Caddy is the only container with published ports, and an app that is ready behind a proxy
# that is not listening is still an outage. A request to 127.0.0.1:80 carries Host:
# 127.0.0.1, which matches no site block, so Caddy answers 404 — that is expected and fine.
# What is being tested is that SOMETHING answered on port 80, with no DNS and no TLS in the
# way to make the result ambiguous.
#
# curl is NOT assumed. A minimal Ubuntu image can be without it, and a missing tool must
# never be reported as "the proxy is down" — `verify` failing is what triggers the rollback
# step in .github/workflows/deploy.yml, so that would roll back a perfectly healthy release
# because a package was absent. bash can open the socket itself; a successful connect proves
# something holds the published port, which is all this check claims.
caddy_probe() {
  if command -v curl >/dev/null 2>&1; then
    curl -sS -o /dev/null --max-time 5 "http://127.0.0.1/" >/dev/null 2>&1
  else
    timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.1/80' 2>/dev/null
  fi
}

wait_for() {
  local path="$1" deadline=$((SECONDS + READY_TIMEOUT)) out=""
  log "waiting up to ${READY_TIMEOUT}s for ${path}"
  while ((SECONDS < deadline)); do
    if out="$(http_probe "$path")"; then
      log "${path} OK: ${out}"
      return 0
    fi
    sleep "$READY_INTERVAL"
  done
  warn "last probe of ${path} said: ${out:-<no output>}"
  warn "---- last 60 lines of the app log ----"
  compose logs --tail=60 --no-color app >&2 2>&1 || true
  return 1
}

wait_ready() { wait_for /readyz; }
wait_live() { wait_for /healthz; }

# ----------------------------------------------------------------------------- the work

pull_image() {
  log "pulling ${APP_IMAGE}:${APP_TAG}"
  # Pulled BEFORE anything is stopped. A bad tag, an expired GHCR PAT or a registry outage
  # must fail while the current version is still serving, not halfway through a recreate.
  docker pull "${APP_IMAGE}:${APP_TAG}" \
    || die "could not pull ${APP_IMAGE}:${APP_TAG}. If that said 'denied' or 'unauthorized', the read PAT has expired — re-run the docker login from CICD.md step D."
}

# `compose run --rm migrate` runs the NEW image's `alembic upgrade head` in a one-shot
# container while the OLD app is still up and serving. That ordering is what lets a broken
# migration fail the deploy with zero downtime — and it is also why a migration has to stay
# readable by the previous release for the seconds between this step and the app restart.
#
# The `up` that follows re-runs the migrate SERVICE (app depends_on it with
# service_completed_successfully, and `run` creates a separate one-off container that does
# not satisfy that condition). The second run is `upgrade head` against an already-current
# database: a no-op costing about a second.
run_migrations() {
  log "starting the database"
  compose up -d db || die "could not start postgres"
  log "running alembic upgrade head"
  compose run --rm migrate \
    || die "the migration failed. NOTHING was deployed — the previous version is still serving. Read the traceback above."
}

bring_up() {
  log "bringing up the stack on ${APP_TAG}"
  compose up -d --remove-orphans
}

# The Caddyfile is a read-only bind mount, and changing a bind-mounted file does NOT cause
# compose to recreate the container — so a Caddyfile change would otherwise sit on disk,
# unused, until the next unrelated restart. `caddy reload` validates the new config and
# swaps it atomically; a bad config is REJECTED and the old one keeps serving, which is why
# this can fail the deploy without taking the site down. It runs after the app is confirmed
# ready, so a proxy-config mistake never triggers an application rollback.
reload_caddy() {
  if [[ -z "$(compose ps -q caddy 2>/dev/null || true)" ]]; then
    log "caddy was not running — 'up' started it with the current Caddyfile, no reload needed"
    return 0
  fi
  log "reloading the Caddyfile"
  if ! compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile; then
    die "the Caddyfile was rejected. Caddy is still serving the PREVIOUS config, so the site is UP — but your proxy changes are not live. Fix the Caddyfile in the repo and push again."
  fi
}

do_rollback() {
  load_state
  [[ -n "$PREVIOUS_TAG" ]] \
    || die "no PREVIOUS_TAG in ${STATE_FILE} — there is nothing to roll back to. This was the first deploy. Fix forward."

  if [[ "$APP_TAG" == "$PREVIOUS_TAG" ]]; then
    log "already on ${PREVIOUS_TAG}; re-applying it anyway (rollback is idempotent)"
  fi

  log "ROLLING BACK ${APP_TAG} -> ${PREVIOUS_TAG}"
  # PREVIOUS_TAG is deliberately NOT moved. Rolling back twice returns to the same
  # known-good tag instead of flip-flopping between two broken ones.
  APP_TAG="$PREVIOUS_TAG"
  save_state

  # The previous image is almost certainly still on disk, so this works even when GHCR is
  # unreachable or the PAT is the thing that broke.
  docker pull "${APP_IMAGE}:${APP_TAG}" || warn "could not re-pull ${APP_IMAGE}:${APP_TAG}; using the local copy"
  compose up -d --remove-orphans \
    || die "the rollback itself failed to start. The site is DOWN. Get on the box: cd ${ROOT_DIR} && ./deploy.sh compose logs app"

  if wait_ready; then
    log "rolled back to ${APP_TAG} and it is serving"
    # Alembic is NOT downgraded. Re-pointing an image tag is reversible; dropping a column
    # is not, and guessing which it was at rollback time is how you lose data. If the new
    # migration is what broke the old app, an explicit `alembic downgrade <rev>` through
    # `./deploy.sh compose run --rm migrate` is a deliberate, manual decision.
    log "note: migrations were NOT downgraded — the schema is still at the new head"
    return 0
  fi
  die "the rollback to ${APP_TAG} ALSO failed to become ready. The site is DOWN."
}

# ------------------------------------------------------------------------- subcommands

cmd_deploy() {
  local ref="${1:-}"
  [[ -n "$ref" ]] || die "usage: $0 deploy <image>:<tag>   e.g. ghcr.io/owner/uktzed-v2:sha-abc1234"

  local new_image="${ref%:*}" new_tag="${ref##*:}"
  [[ "$ref" == *:* && "$new_tag" != */* && -n "$new_image" && -n "$new_tag" ]] \
    || die "'${ref}' has no tag. Deploy an immutable tag (sha-<short>), never a bare name and never ':latest' — ':latest' cannot be rolled back to anything."

  preflight
  take_lock
  load_state

  local outgoing="$APP_TAG"
  log "=== deploy ${new_image}:${new_tag} (replacing ${outgoing:-<nothing>}) ==="

  # Pull first. Nothing is committed to disk or to the state file until the image is here.
  APP_IMAGE="$new_image"
  APP_TAG="$new_tag"
  pull_image

  # Migrations run against the OLD stack, so a failure here has changed nothing at all —
  # which is why the state file is still untouched at this point. Writing it any earlier
  # would leave deploy.state claiming a tag that is not running, and `status` would then
  # report a version the box has never served.
  run_migrations

  # Recorded BETWEEN the migration and the recreate: late enough that a failed migration
  # leaves no trace, early enough that everything from here on is rollback-able.
  # PREVIOUS_TAG only moves when there is somewhere to move to — re-deploying the same tag
  # must not overwrite the known-good rollback target with itself.
  if [[ -n "$outgoing" && "$outgoing" != "$new_tag" ]]; then
    PREVIOUS_TAG="$outgoing"
  fi
  save_state

  if ! bring_up; then
    warn "'compose up' failed on ${new_tag}"
    do_rollback
    die "${new_image}:${new_tag} could not be started and was rolled back to ${APP_TAG}."
  fi

  if is_bootstrapped; then
    if ! wait_ready; then
      warn "${new_image}:${new_tag} never reported ready"
      do_rollback
      die "${new_image}:${new_tag} failed readiness and was rolled back to ${APP_TAG}."
    fi
  else
    # First deploy on this box: /readyz is 503 until the tariff is ingested, so liveness is
    # the only honest gate. See THE BOOTSTRAP FLAG at the top of this file.
    if ! wait_live; then
      warn "${new_image}:${new_tag} never came up at all"
      do_rollback
      die "${new_image}:${new_tag} failed liveness and was rolled back to ${APP_TAG}."
    fi
    log "the app is LIVE but not READY: the tariff has never been ingested on this box."
    log "  next:  ${ROOT_DIR}/deploy.sh ingest"
    log "  then:  ${ROOT_DIR}/deploy.sh compose exec app python -m app.cli create-user <name> --superuser"
  fi

  reload_caddy

  log "=== deployed ${APP_IMAGE}:${APP_TAG} (rollback target: ${PREVIOUS_TAG:-none}) ==="
}

cmd_rollback() {
  preflight
  take_lock
  do_rollback
}

cmd_verify() {
  preflight
  load_state
  [[ -n "$APP_TAG" ]] || die "nothing has been deployed yet (${STATE_FILE} is missing or empty)"

  local out
  if out="$(readyz_probe)"; then
    log "readyz OK: ${out}"
  elif is_bootstrapped; then
    warn "readyz: ${out}"
    die "/readyz is not 200 on ${APP_IMAGE}:${APP_TAG}"
  else
    # Not a failure yet — nothing has ever been ingested here, so 503 is the correct answer
    # and there is no earlier version this could have regressed from.
    warn "readyz: ${out}"
    warn "the tariff has never been ingested on this box. Run: ${ROOT_DIR}/deploy.sh ingest"
    log "liveness and proxy are still checked below"
    healthz_probe || die "/healthz is not 200 either — the app is not serving at all"
  fi

  if ! caddy_probe; then
    die "nothing answered on 127.0.0.1:80 — the app is ready but caddy is not serving it, so the site is down from the outside"
  fi
  log "caddy is listening on :80"

  # The only check that leaves the machine, and the only one that is a warning. It exits the
  # VPS and comes back to it, so a failure here can equally mean hairpin NAT, a DNS record
  # that has not propagated or a firewall — none of which is a reason to roll back a healthy
  # release. Load the site in a browser to settle it.
  local domain code
  domain="$(grep -E '^UKTZED_DOMAIN=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  if [[ -n "$domain" && "$domain" != "localhost" ]] && command -v curl >/dev/null 2>&1; then
    if code="$(curl -sS -o /dev/null --max-time 15 -w '%{http_code}' "https://${domain}/readyz" 2>/dev/null)"; then
      log "https://${domain}/readyz answered HTTP ${code}"
    else
      warn "https://${domain}/readyz did not answer from the VPS itself. Inconclusive (hairpin NAT / DNS / firewall) — check it from a browser."
    fi
  fi

  log "verify OK: ${APP_IMAGE}:${APP_TAG}"
}

cmd_status() {
  preflight
  load_state
  echo "image          ${APP_IMAGE:-<unset>}"
  echo "tag            ${APP_TAG:-<unset>}"
  echo "rollback to    ${PREVIOUS_TAG:-<none recorded>}"
  echo "bootstrapped   $(is_bootstrapped && echo "yes — /readyz is a hard gate" || echo "NO — run './deploy.sh ingest'")"
  echo ""
  compose ps || true
  echo ""
  readyz_probe || true
}

# The explicit data step. Idempotent per content hash: re-running it against an unchanged
# data/uktzed_hierarchical.json updates nothing and prints the same counts.
# Expect 14187 nodes / 10490 terminals.
cmd_ingest() {
  preflight
  take_lock
  load_state
  [[ -n "$APP_TAG" ]] || die "nothing is deployed yet; deploy before ingesting"
  local file="${1:-$TARIFF_DEFAULT}"
  log "ingesting ${file} (the file ships inside the image, at /srv/${file})"
  compose run --rm app python -m app.cli ingest "$file" || die "the ingest failed; nothing was bootstrapped"

  # Arms the hard /readyz gate for every future deploy and verify. Only written after the
  # ingest actually succeeded — an empty flag file promising a tariff that is not there
  # would turn the first real regression into a silent one.
  if ! is_bootstrapped; then
    touch "$BOOTSTRAP_FLAG"
    log "bootstrapped. From now on a deploy that does not reach /readyz 200 is rolled back."
  fi
  readyz_probe || warn "/readyz is still not 200 — check './deploy.sh compose logs app'"
}

# Old sha- tags pile up and each is a few hundred MB. Keeps exactly what rollback needs:
# the live tag and PREVIOUS_TAG. Never run this to "free space" during an incident — it is
# what would delete the image you are about to roll back to.
cmd_prune() {
  preflight
  load_state
  [[ -n "$APP_IMAGE" ]] || die "no APP_IMAGE recorded"

  local keep_live="${APP_IMAGE}:${APP_TAG}" keep_prev=""
  if [[ -n "$PREVIOUS_TAG" ]]; then
    keep_prev="${APP_IMAGE}:${PREVIOUS_TAG}"
  fi

  local ref
  while read -r ref; do
    [[ -n "$ref" ]] || continue
    if [[ "$ref" == "$keep_live" || "$ref" == "$keep_prev" ]]; then
      log "keeping ${ref}"
      continue
    fi
    log "removing ${ref}"
    docker rmi "$ref" || warn "could not remove ${ref} (still in use?)"
  done < <(docker images --format '{{.Repository}}:{{.Tag}}' "$APP_IMAGE")
}

cmd_compose() {
  preflight
  load_state
  [[ -n "$APP_TAG" ]] || die "nothing is deployed yet, so there is no image tag to interpolate"
  compose "$@"
}

usage() {
  cat <<'USAGE'
UKTZED v2 deploy — runs on the VPS, as root, from /root/uktzed-v2

  ./deploy.sh deploy <image>:<tag>   pull, migrate, up, wait for /readyz, roll back on failure
  ./deploy.sh rollback               re-point to PREVIOUS_TAG and bring it back up
  ./deploy.sh verify                 /readyz + caddy, right now
  ./deploy.sh status                 what is deployed, what rollback would go to, container state
  ./deploy.sh ingest [file]          load the tariff — a DATA step, never automatic
  ./deploy.sh prune                  delete old image tags, keeping live + rollback target
  ./deploy.sh compose <args...>      any docker compose command, with the right -f and env

Day two:
  ./deploy.sh compose logs -f app
  ./deploy.sh compose exec app python -m app.cli create-user anton --superuser
  ./deploy.sh compose exec db psql -U postgres uktzed
USAGE
}

main() {
  local sub="${1:-}"
  if [[ $# -gt 0 ]]; then
    shift
  fi
  case "$sub" in
    deploy) cmd_deploy "$@" ;;
    rollback) cmd_rollback ;;
    verify) cmd_verify ;;
    status) cmd_status ;;
    ingest) cmd_ingest "$@" ;;
    prune) cmd_prune ;;
    compose) cmd_compose "$@" ;;
    "" | -h | --help | help) usage ;;
    *)
      usage
      die "unknown subcommand: ${sub}"
      ;;
  esac
}

main "$@"
