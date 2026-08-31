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


settings = Settings()
