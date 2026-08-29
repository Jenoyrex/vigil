# Vigil API

`apps/api` is the FastAPI backend for Vigil. It is an independent Python project managed with
[uv](https://github.com/astral-sh/uv); it does not share a workspace or dependency lockfile with
any other part of the monorepo.

At this stage it exposes a health check endpoint and the initial PostgreSQL schema (users,
organizations, memberships, projects, API keys) via SQLAlchemy + Alembic. No API-key
authentication, background jobs, or business logic has been added yet — this is schema
foundation only.

## Requirements

- Python 3.12 (pinned in `.python-version`)
- [uv](https://github.com/astral-sh/uv)
- Docker (for the local PostgreSQL database, via `infrastructure/docker-compose.yml`)

## Install dependencies

From `apps/api`:

```bash
uv sync
```

This creates a local `.venv` and installs both runtime and development dependencies.

## Database setup

Start a local PostgreSQL instance from `infrastructure`:

```bash
cd infrastructure
docker compose up -d postgres
```

This starts Postgres on `localhost:5434` (chosen to avoid colliding with any other local Postgres
on the default `5432`) with a `vigil` database for development and a `vigil_test` database for
tests, both created automatically. Override credentials/port via `infrastructure/.env` (see
`infrastructure/.env.example`) rather than editing `docker-compose.yml`.

Copy `apps/api/.env.example` to `apps/api/.env` and adjust if you changed the defaults.

Apply migrations (from `apps/api`):

```bash
uv run alembic upgrade head
```

This must be run against both the `vigil` database (used by the app) and the `vigil_test`
database (used by the test suite) — point `VIGIL_API_DATABASE_URL` at each in turn, or use
`VIGIL_API_DATABASE_URL=<vigil_test URL> uv run alembic upgrade head` for the second one.

### Creating new migrations

```bash
uv run alembic revision --autogenerate -m "describe the change"
```

Always review generated migrations before applying them.

### updated_at strategy

`updated_at` is maintained by SQLAlchemy's `onupdate=func.now()` (see `app/db/base.py`), not a
PostgreSQL trigger: the ORM includes `now()` in the `UPDATE` statement it issues, so the timestamp
is still computed by the database (avoiding client clock skew) while the decision to update it
stays at the application layer. This only refreshes `updated_at` for writes made through
SQLAlchemy — a documented, accepted limitation while all writes go through the ORM.

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

Requires the `vigil_test` database (see Database setup above) with migrations applied. From
`apps/api`:

```bash
uv run pytest
```

Tests run against `VIGIL_API_TEST_DATABASE_URL` (defaults to the local `vigil_test` database) and
never touch the `vigil` development database. `tests/conftest.py` refuses to run if that URL
doesn't point at a database whose name ends in `test`, and truncates all tables before and after
each test for isolation.

## Run Ruff

From `apps/api`:

```bash
uv run ruff check .
```

`alembic/versions/` is excluded from linting since those files are Alembic-generated.
