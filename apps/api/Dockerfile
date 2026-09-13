# syntax=docker/dockerfile:1
#
# Production image for apps/api (FastAPI + uvicorn). Build context must be
# apps/api itself (this project has no local path dependency on any other
# part of the monorepo, unlike services/worker) -- see docs/decisions for
# the Phase 4B deployment ADR.
#
# Two stages: `builder` resolves and installs the exact dependency set
# pinned in the committed uv.lock into a venv; `runtime` copies only that
# venv plus the application source into a slim, non-root final image. No
# secret ever appears in a RUN/ENV/ARG instruction -- database URLs, API
# keys, and the internal service token are all supplied at `docker run`/
# `docker compose up` time via environment variables, never baked in here.

FROM python:3.12-slim-bookworm AS builder

# Pinned uv version, installed as a static binary (no pip/pyproject
# resolution of uv itself needed).
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv

WORKDIR /build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Install dependencies first, separately from application source, so
# source-only changes don't invalidate the dependency-install layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY README.md ./README.md
RUN uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm AS runtime

RUN groupadd --system --gid 1000 vigil \
    && useradd --system --uid 1000 --gid vigil --no-create-home vigil

WORKDIR /app

COPY --from=builder --chown=vigil:vigil /build/.venv /app/.venv
COPY --from=builder --chown=vigil:vigil /build/app /app/app
COPY --from=builder --chown=vigil:vigil /build/alembic /app/alembic
COPY --from=builder --chown=vigil:vigil /build/alembic.ini /app/alembic.ini

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER vigil

EXPOSE 8000

# /health is a pure liveness check (never touches a datastore); the
# container healthcheck instead targets /ready deliberately, so an
# unhealthy container accurately reflects "can't reach ClickHouse or
# PostgreSQL" rather than only "process is alive" (see apps/api/app/main.py).
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3).status == 200 else 1)"

# Invoked via `python -m uvicorn`, not the `uvicorn` console-script wrapper:
# uv-created venv console scripts get a shebang hardcoded to the path they
# were built at (/build/.venv/bin/python, the builder stage's WORKDIR),
# which does not exist in this runtime stage (only /app/.venv does) --
# `python -m` sidesteps the wrapper script (and its broken shebang)
# entirely by invoking the real interpreter directly.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
