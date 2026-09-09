"""services/worker settings, same `pydantic-settings`/env-var pattern as
`apps/api/app/config.py` (see docs/decisions/005-evaluation-job-storage-worker.md
section 12), scoped for now to what has actually been built: ClickHouse
`evaluation_results` storage, PostgreSQL `evaluation_jobs` lifecycle/
claiming, `worker.dispatcher.Dispatcher`'s bounded concurrency, and
`worker.failure_handling`'s retry/backoff. Reaper/poller settings
(`evaluator_call_timeout_seconds`, `stuck_job_threshold_seconds`, etc.)
belong to the later phases that introduce that behavior, not here.
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


settings = Settings()
