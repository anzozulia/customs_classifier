# UKTZED v2 image: one image runs the API, the one-shot migrator and the CLI.
#
# Three stages:
#   frontend  node:22-alpine   -> the Vite bundle
#   pybuild   python:3.13-slim -> a venv with every runtime dependency (needs git)
#   runtime   python:3.13-slim -> venv + source + data + bundle, non-root, no toolchain

# =============================================================================
# stage 1 — frontend (React + Vite + Tailwind, D4)
# =============================================================================
FROM node:22-alpine AS frontend

WORKDIR /fe

# web/package-lock.json MUST be committed: `npm ci` fails without it, by design —
# that is the whole point of using ci instead of install.
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./

# No VITE_* build args, deliberately. The ChatKit domain key is a PUBLIC value, but
# inlining it at build time is what forces a frontend rebuild every time the domain
# changes. v2 serves it at runtime from GET /api/config instead (D4), so this stage
# takes no configuration at all and its cache never invalidates on an env change.
RUN npm run build
# -> /fe/dist

# =============================================================================
# stage 2 — python dependencies, built into a relocatable venv
# =============================================================================
FROM python:3.13-slim AS pybuild

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# git is REQUIRED at build time: openai-chatkit is not on PyPI (the `chatkit` name
# there is a reserved 0.0.1 stub), so pyproject.toml installs it from a git tag.
# build-essential is insurance for any sdist-only dependency. Both are confined to
# this stage — the runtime image below copies out only the finished venv.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml ./

# pyproject.toml is the single source of truth for the pins, so the pins are read
# out of it instead of being duplicated into a requirements.txt that would drift.
#
# The project itself is deliberately NOT `pip install .`-ed: pyproject declares no
# [build-system] and the repo is a flat layout (app/ data/ evals/ migrations/
# scripts/ tests/ web/), so setuptools auto-discovery has nothing unambiguous to
# find. Running from /srv with the source on the path is what local dev does too —
# one fewer thing that can behave differently in the container.
RUN python -c "import tomllib;print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt

# Alembic needs a SYNCHRONOUS DBAPI. The app's runtime driver is asyncpg, which
# SQLAlchemy can only drive through its async engine, and migrations are a one-shot
# job that must not borrow the app's pool (migrations/env.py explains why). psycopg3
# ships manylinux binary wheels, so this adds no compiler requirement.
# It belongs in pyproject.toml's dependency list with an exact pin; it is here only
# because that file is owned elsewhere. Move it the next time you touch the pins.
RUN pip install "psycopg[binary]>=3.2,<4"

# =============================================================================
# stage 3 — runtime
# =============================================================================
FROM python:3.13-slim AS runtime

# TZ=UTC        the ChatKit SDK uses naive datetime.now() everywhere and our
#               pagination compares created_at — mixing naive and aware raises
#               TypeError, and it does so in the middle of a stream.
# LOG_LEVEL     chatkit/logger.py attaches NO handler unless this is set, so every
#               swallowed respond() traceback would go nowhere. .env overrides it.
# PYTHONUNBUFFERED  otherwise logs arrive in 8 KB chunks long after the events.
# OPENAI_AGENTS_DISABLE_TRACING  v1 shipped 5,482 trace batches offsite with zero
#               consumers, including customer product descriptions (D32).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    LOG_LEVEL=INFO \
    OPENAI_AGENTS_DISABLE_TRACING=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /srv

RUN adduser --system --group --no-create-home app

COPY --from=pybuild /opt/venv /opt/venv

COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY app/ ./app/
# 2.4 MB of tariff JSON, committed on purpose: v1 .gitignore'd it and became a
# wrapper around a file nobody could version.
COPY data/ ./data/
# Same path a local `npm run build` writes to, so the app can mount web/dist in
# development and in the image with one line and no branch.
COPY --from=frontend /fe/dist ./web/dist

# A public build stamp, never a secret — anything in ARG/ENV lands in image layers.
#   docker compose build --build-arg APP_VERSION="$(git describe --always --dirty)"
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# Everything above is owned by root and read-only to this user. Nothing in the
# request path writes to the filesystem.
USER app

EXPOSE 8000

# docker-compose.yml overrides this with the production flags (workers, proxy
# headers, shutdown timeouts). This default is the plain `docker run` form.
CMD ["uvicorn", "app.main:app", "--host=0.0.0.0", "--port=8000"]
