from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIGIL_API_", env_file=".env")

    app_name: str = "Vigil API"
    database_url: str = "postgresql+psycopg://vigil:vigil@localhost:5434/vigil"

    # ClickHouse connection. Defaults match infrastructure/.env.example /
    # infrastructure/docker-compose.yml local development credentials.
    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_database: str = "vigil"
    clickhouse_user: str = "vigil"
    clickhouse_password: str = "vigil"
    clickhouse_timeout_seconds: float = 10.0

    # Telemetry ingestion payload limits, per
    # docs/decisions/003-clickhouse-telemetry-storage.md.
    max_spans_per_request: int = 1000
    max_request_body_bytes: int = 10 * 1024 * 1024  # 10 MiB
    max_input_bytes: int = 64 * 1024
    max_output_bytes: int = 64 * 1024
    max_total_span_bytes: int = 256 * 1024

    # Query API (read-side) safety limits, for the Trace Explorer/analytics
    # endpoints (GET /v1/traces*, GET /v1/analytics/*). These bound every
    # list/analytics query to a time window that can never accidentally
    # scan the full retention window, and cap how many spans one trace
    # detail response returns.
    max_query_window_days: int = 7
    default_query_window_hours: int = 24
    max_spans_per_trace_response: int = 2000


settings = Settings()
