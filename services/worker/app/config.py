"""services/worker settings, same `pydantic-settings`/env-var pattern as
`apps/api/app/config.py` (see docs/decisions/005-evaluation-job-storage-worker.md
section 12), scoped for now to what Phase 2 (ClickHouse `evaluation_results`
storage only) actually needs. Job-claiming/dispatch settings
(`max_concurrent_evaluations`, `evaluator_call_timeout_seconds`, etc.) belong
to the later phase that introduces the claim loop and dispatch, not here.
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


settings = Settings()
