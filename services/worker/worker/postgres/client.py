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

Phase 4D closes the I/O-timeout gap `worker/execution.py`'s own docstring
has documented since Phase 4A ("no `connect_timeout` and no statement
timeout of any kind"), mirroring `worker/clickhouse/client.py`'s existing
`connect_timeout`/`send_receive_timeout` pattern for the PostgreSQL side:

- `connect_timeout` (a genuine libpq connection parameter, enforced at the
  TCP/authentication-handshake level, independent of server cooperation)
  bounds how long establishing a new connection can take.
- PostgreSQL's own `statement_timeout` session parameter, set via the
  `options` connection parameter (the standard, documented psycopg idiom
  for setting a session-level GUC at connect time -- not a client-side
  guess), bounds how long the SERVER will let any one statement run before
  cancelling it and returning `psycopg.errors.QueryCanceled` to this
  client. This is a real, server-enforced bound: a stuck query is
  genuinely cancelled, not just a client that gives up waiting.

Both are bounded by the SAME `database_timeout_seconds` setting (like
ClickHouse's single `clickhouse_timeout_seconds`, not two separate knobs)
-- there is no evidence yet that connection-establishment and
statement-execution need materially different bounds for this workload,
and a single setting is simpler to reason about and configure.

`QueryCanceled`/`OperationalError` raised by either bound are not one of
`worker.failure_handling`'s two permanent exception types
(`SourceSpanNotFoundError`, `InvalidEvaluatorInputError`), so they are
retryable by default, same as any other PostgreSQL failure -- see that
module's docstring. `worker.dispatcher.Dispatcher._run_one`/`_handle_failure`
and `worker.runtime.WorkerRuntime._claim_and_dispatch`/`_reap` already
catch and gracefully handle an exception raised at any point a PostgreSQL
call could fail (including compounded failures, e.g. the *recording* of a
failure also timing out) -- this file only had to make sure a bound
actually exists for them to eventually catch; no other module needed to
change for retry/dead-letter semantics to keep working correctly.

One residual, explicitly accepted gap: `statement_timeout` depends on the
PostgreSQL server successfully sending its cancellation response back to
this client. A network partition occurring *after* a connection is already
established, where that response itself never arrives, could in principle
still block past this bound -- closing that fully would need either
OS-level `tcp_user_timeout` tuning or wrapping every call in a thread-based
timeout (the same "abandon, don't kill" tradeoff `worker/timeouts.py`
already accepts for evaluator calls), which is more machinery than this
narrow fix's scope justifies. `connect_timeout` has no such caveat --
that phase is unconditionally, OS-level bounded.
"""

from __future__ import annotations

import psycopg

from worker.config import settings


def get_connection() -> psycopg.Connection:
    return psycopg.connect(
        settings.database_url,
        autocommit=True,
        connect_timeout=int(settings.database_timeout_seconds),
        options=f"-c statement_timeout={int(settings.database_timeout_seconds * 1000)}",
    )
