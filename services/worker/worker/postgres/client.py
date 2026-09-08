"""PostgreSQL connection construction for services/worker.

Raw `psycopg`, never SQLAlchemy or any import from `apps/api`'s ORM models
-- per ADR 001 decision 6 ("duplicate rather than centralize") and
docs/decisions/005-evaluation-job-storage-worker.md section 6, the worker's
`evaluation_jobs` lifecycle access is deliberately independent of
`apps/api`'s data-access layer.

One new connection per call, `autocommit=True`. Every
`EvaluationJobsRepository` method (see `repository.py`) is exactly one SQL
statement -- a single `UPDATE`, already atomic on its own regardless of
transaction mode. Autocommit means no explicit `BEGIN`/`COMMIT` bookkeeping
is needed anywhere in this module: each statement is its own transaction,
which is exactly the boundary the Phase 3 plan calls for (a claim is its
own transaction; a completion transition is a separate one -- never one
transaction spanning an evaluator call). Not pooled: claim/completion calls
are comparatively low-frequency (once per batch, once per job) next to a
hot per-request path like `apps/api`'s ClickHouse client; a pool can be
introduced later if connection-open overhead is ever actually measured to
matter.
"""

from __future__ import annotations

import psycopg

from worker.config import settings


def get_connection() -> psycopg.Connection:
    return psycopg.connect(settings.database_url, autocommit=True)
