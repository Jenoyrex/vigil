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

# No EXPOSE / HEALTHCHECK: neither the runtime loop nor the poller runs an
# HTTP server (both are long-running polling loops -- see
# docs/decisions/005-evaluation-job-storage-worker.md). Liveness is
# enforced entirely by the Compose `restart: unless-stopped` policy, which
# is also the external supervisor Phase 4A's bounded-orphan self-retirement
# (worker/runtime.py) depends on to bring the process back after it
# retires itself.

CMD ["python", "-m", "worker"]
