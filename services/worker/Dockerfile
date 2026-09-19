# syntax=docker/dockerfile:1
#
# Production image for services/worker. Serves BOTH process entrypoints
# that live in this one package -- `python -m worker` (claim/dispatch/reap
# runtime) and `python -m worker.poller_main` (ClickHouse-scanning job
# creation poller) -- selected at `docker run`/Compose time via `command:`,
# not by building two separate images (see the Phase 4B deployment ADR for
# why: same package, same flat dependency list, no functional benefit to
# splitting).
#
# services/worker/pyproject.toml declares a LOCAL PATH DEPENDENCY on
# services/evaluator ([tool.uv.sources] vigil-evaluator = { path =
# "../evaluator" }). This build's primary context is services/worker itself
# (so this directory's own .dockerignore applies normally); services/
# evaluator is supplied as a separate, NAMED additional build context
# (BuildKit `--build-context`, Compose `build.additional_contexts`) called
# "evaluator", copied into the build at /build/evaluator -- the same
# position "../evaluator" resolves to relative to /build/worker below, so
# uv.lock's `source = { directory = "../evaluator" }` entry resolves
# exactly as it does on a developer's machine.
#
# Standalone build (from repo root):
#   docker build -f services/worker/Dockerfile \
#     --build-context evaluator=services/evaluator \
#     services/worker

FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv

WORKDIR /build/worker

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=evaluator . /build/evaluator

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY worker ./worker
COPY README.md ./README.md
RUN uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm AS runtime

RUN groupadd --system --gid 1000 vigil \
    && useradd --system --uid 1000 --gid vigil --no-create-home vigil

WORKDIR /app

COPY --from=builder --chown=vigil:vigil /build/worker/.venv /app/.venv
COPY --from=builder --chown=vigil:vigil /build/worker/worker /app/worker

# `vigil` is created with --no-create-home, so it has no writable $HOME --
# services/evaluator/app/embedding_relevance.py's EmbeddingRelevanceEvaluator
# defaults its `fastembed` model-weights cache under `Path.home()`, which
# would fail every write with a permission error the first time
# `relevance_embedding` runs in this container. Pre-create a worker-owned,
# writable cache directory under /app for it, and point
# VIGIL_EVALUATOR_EMBEDDING_CACHE_DIR (services/evaluator's own override
# hook) at it below. That alone is not sufficient, though: `huggingface_hub`'s
# `xet` accelerated-download backend also writes its own log files straight to
# `$HOME/.cache/huggingface/xet/logs`, independent of fastembed's `cache_dir`
# argument, so a still-unset/unwritable $HOME hits the exact same permission
# error one level deeper. Fixing that means giving `vigil` an actual writable
# $HOME rather than chasing every library's own ancillary cache path
# one at a time, so HOME is repointed at /app -- everything under it is
# owned by `vigil` (chowned below, while still root, since `vigil` cannot
# chown a directory it doesn't already own).
RUN mkdir -p /app/.cache/vigil-evaluator/fastembed \
    && chown -R vigil:vigil /app/.cache

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOME=/app \
    VIGIL_EVALUATOR_EMBEDDING_CACHE_DIR=/app/.cache/vigil-evaluator/fastembed

USER vigil

# No EXPOSE: neither the runtime loop nor the poller runs an HTTP server
# (both are long-running polling loops -- see docs/decisions/
# 005-evaluation-job-storage-worker.md). Restart-on-crash is still
# enforced entirely by the Compose `restart: unless-stopped` policy, which
# is also the external supervisor Phase 4A's bounded-orphan self-retirement
# (worker/runtime.py) depends on to bring the process back after it
# retires itself.
#
# HEALTHCHECK (Phase 4D, F6) closes the "Docker can only ever see 'process
# is running', never genuine liveness" gap docs/decisions/
# 006-deployment-architecture.md's own Known Limitations named -- without
# an HTTP server to probe (see above), this checks worker/heartbeat.py's
# heartbeat file instead: both `python -m worker`
# (worker/runtime.py's WorkerRuntime.run) and
# `python -m worker.poller_main` (worker/poller.py's Poller.run) touch the
# exact same path once per completed loop iteration, so this single
# instruction correctly covers whichever of the two this container is
# running via its own `command:` override. A plain Python one-liner, not
# curl/wget -- Python is already installed and importable with zero extra
# image layer, and this check needs no network client at all. The
# staleness bound is read from VIGIL_WORKER_HEARTBEAT_STALE_SECONDS
# (worker/config.py's heartbeat_stale_seconds, default 180s) rather than
# hardcoded here, so an operator who raises evaluator_init_timeout_seconds
# or poller_interval_seconds materially doesn't get spurious unhealthy
# reports -- deliberately read via a bare os.environ.get, not by importing
# worker.config.Settings, so this check stays a fast, dependency-light
# subprocess rather than paying this image's full application-import cost
# on every tick.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "\
import os, sys, time; \
path = '/tmp/vigil-worker-heartbeat'; \
stale_after = float(os.environ.get('VIGIL_WORKER_HEARTBEAT_STALE_SECONDS', '180')); \
sys.exit(0 if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < stale_after else 1)"

CMD ["python", "-m", "worker"]
