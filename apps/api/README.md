# Vigil API

`apps/api` is the FastAPI backend for Vigil. It is an independent Python project managed with
[uv](https://github.com/astral-sh/uv); it does not share a workspace or dependency lockfile with
any other part of the monorepo.

At this stage it exposes only a health check endpoint. No database, background job, or
authentication logic has been added yet.

## Requirements

- Python 3.12 (pinned in `.python-version`)
- [uv](https://github.com/astral-sh/uv)

## Install dependencies

From `apps/api`:

```bash
uv sync
```

This creates a local `.venv` and installs both runtime and development dependencies.

## Run the API locally

From `apps/api`:

```bash
uv run uvicorn app.main:app --reload
```

The API will be available at `http://127.0.0.1:8000`. Check the health endpoint:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}
```

## Run tests

From `apps/api`:

```bash
uv run pytest
```

## Run Ruff

From `apps/api`:

```bash
uv run ruff check .
```
