"""services/worker settings, same `pydantic-settings`/env-var pattern as
`apps/api/app/config.py` (see docs/decisions/005-evaluation-job-storage-worker.md
section 12), scoped for now to what has actually been built: ClickHouse
`evaluation_results` storage, PostgreSQL `evaluation_jobs` lifecycle/
claiming, `worker.dispatcher.Dispatcher`'s bounded concurrency,
`worker.failure_handling`'s retry/backoff, `worker.reaper`'s stuck-job
reclaim, and `worker.runtime.WorkerRuntime`'s claim/dispatch/reap loop.
`evaluator_call_timeout_seconds` (a per-call evaluator timeout) remains
unimplemented anywhere in this codebase -- a future phase, not this one; see
`worker/runtime.py`'s module docstring for the known limitation that leaves
open.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIGIL_WORKER_", env_file=".env")

    # ClickHouse connection. Defaults match infrastructure/.env.example /
    # infrastructure/docker-compose.yml local development credentials --
    # same defaults apps/api/app/config.py uses for the same database.
    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_database: str = "vigil"
    clickhouse_user: str = "vigil"
    clickhouse_password: str = "vigil"
    clickhouse_timeout_seconds: float = 10.0

    # PostgreSQL connection -- raw psycopg, never SQLAlchemy/apps/api's ORM
    # (ADR 001 decision 6, ADR 005 section 6). A plain `postgresql://` DSN
    # (psycopg's own conninfo format), not apps/api's SQLAlchemy-dialect-
    # prefixed `postgresql+psycopg://` one, though it points at the same
    # local database by default.
    database_url: str = "postgresql://vigil:vigil@localhost:5434/vigil"

    # Bounded-concurrency dispatch (worker/dispatcher.py). Deliberately
    # small by default, not unbounded -- ADR 005 section 12's "a project
    # operator... should not silently get full-volume evaluation" posture,
    # applied here to worker-process resource usage rather than per-project
    # sampling. Must be >= 1; Dispatcher itself also enforces this.
    max_concurrent_evaluations: int = 4

    # Retry/backoff (worker/failure_handling.py), per
    # docs/decisions/005-evaluation-job-storage-worker.md's Phase 3E
    # amendment: delay_seconds = min(retry_max_delay_seconds,
    # retry_base_seconds * (2 ** (attempt_count - 1))) +
    # random.uniform(0, retry_jitter_seconds). Attempt 1 -> ~5s, attempt 2
    # -> ~10s, attempt 3 -> ~20s, ... capped at 300s before jitter.
    retry_base_seconds: float = 5.0
    retry_max_delay_seconds: float = 300.0
    retry_jitter_seconds: float = 2.0

    # Stuck-job reaper (worker/reaper.py), per docs/decisions/005-evaluation-
    # job-storage-worker.md's Phase 3F amendment. stuck_job_threshold_seconds
    # must be meaningfully larger than however long a genuine, alive
    # evaluation can take, so the reaper only catches real process death,
    # never a call that's merely slow -- no per-call evaluator timeout is
    # enforced yet (see this module's own docstring), so this default is
    # deliberately conservative until one exists. reaper_batch_size mirrors
    # claim_jobs' own batch_size argument, given a config default here since,
    # unlike claim_jobs' worker-loop caller, the reaper has no other natural
    # source for it.
    stuck_job_threshold_seconds: float = 900.0
    reaper_batch_size: int = 100

    # Worker runtime loop (worker/runtime.py), per docs/decisions/005-
    # evaluation-job-storage-worker.md's Phase 3G amendment. claim_batch_size
    # is deliberately independent of max_concurrent_evaluations -- both
    # default to 4 today, but they are separate knobs (how many jobs one
    # claim tick pulls vs. how many `Dispatcher` runs concurrently) and are
    # never coupled in code, so either can be retuned without touching the
    # other. poll_interval_seconds is how long an idle claim tick waits
    # before trying again; reaper_interval_seconds is the runtime loop's own,
    # separate cadence for calling reap_stuck_jobs, tracked via
    # time.monotonic() rather than wall-clock time.
    claim_batch_size: int = 4
    poll_interval_seconds: float = 2.0
    reaper_interval_seconds: float = 60.0


settings = Settings()
