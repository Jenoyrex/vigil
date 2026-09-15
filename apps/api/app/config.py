import logging

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIGIL_API_", env_file=".env")

    app_name: str = "Vigil API"
    database_url: str = "postgresql+psycopg://vigil:vigil@localhost:5434/vigil"

    # Structured logging (Phase 4D, F4, app/logging_config.py). Standard
    # Python logging level name -- validated below so a typo fails loudly at
    # process start (the same posture `cors_allowed_origins_list` already
    # takes for a dangerous misconfiguration) rather than silently falling
    # back to WARNING, which `logging.Logger.setLevel` would otherwise do
    # for an unrecognized string.
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        valid_levels = logging.getLevelNamesMapping()
        if normalized not in valid_levels:
            raise ValueError(
                f"VIGIL_API_LOG_LEVEL={value!r} is not a valid logging level "
                f"(expected one of {sorted(valid_levels)})."
            )
        return normalized

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

    # Per-API-key in-process token-bucket rate limiting (Phase 4C), keyed by
    # AuthenticatedKey.api_key_id -- see app/api/rate_limit.py. Two tiers:
    # a stricter one for POST /v1/traces (the highest-volume, highest-cost
    # write path) and a more generous one shared by every other
    # authenticated customer endpoint. Capacity is the burst size (tokens
    # available immediately); refill_per_second is the sustained rate once
    # the burst is spent. These are a starting point, not load-tested
    # production numbers -- there is no production traffic history yet to
    # calibrate against; revisit once there is. max_tracked_api_keys bounds
    # this process's memory to at most that many concurrently-tracked keys
    # (least-recently-used eviction beyond that), independent of how many
    # distinct API keys actually exist.
    rate_limit_ingestion_capacity: int = 20
    rate_limit_ingestion_refill_per_second: float = 5.0
    rate_limit_default_capacity: int = 60
    rate_limit_default_refill_per_second: float = 20.0
    rate_limit_max_tracked_api_keys: int = 10_000

    # Explicit deny-by-default CORS policy (Phase 4C) -- see docs/decisions/
    # 007-cors-and-dashboard-security-headers.md. Empty by default: this API
    # has no legitimate browser-based cross-origin consumer today -- the
    # dashboard is a server-side BFF (apps/dashboard/lib/api/vigilClient.ts),
    # never calling this API directly from browser JS -- so denying all
    # cross-origin browser access is the correct default, not a gap to fill
    # in later. A comma-separated list of exact origins (scheme + host +
    # port, e.g. "https://app.example.com,https://admin.example.com"); set
    # only if a future browser-based consumer is introduced. See
    # `cors_allowed_origins_list` below for parsing/validation.
    cors_allowed_origins: str = ""

    # Internal worker-fleet authentication (POST /v1/evaluations/jobs), per
    # docs/decisions/005-evaluation-job-storage-worker.md section 9 / Phase
    # 3H amendment. Deliberately no default -- must be supplied via
    # environment/.env in every environment, local development included,
    # never baked into source. services/worker's own Settings holds the
    # same value under its own VIGIL_WORKER_ prefix.
    internal_service_token: str

    # Production provisioning/onboarding bootstrap (Phase 4D, F3) -- see
    # app/api/v1/provisioning.py and docs/decisions/006-deployment-
    # architecture.md. Empty by default, which `app.api.deps.
    # get_bootstrap_auth` treats as "bootstrap is disabled": every request
    # to `POST /v1/provisioning/bootstrap` is rejected with 401, regardless
    # of any token presented, unless an operator explicitly sets this to a
    # real, high-entropy secret. Deliberately no non-empty default -- unlike
    # `internal_service_token` above (which every environment, local dev
    # included, MUST set or the app refuses to start), bootstrap is meant to
    # be unreachable by default and only enabled for the brief window an
    # operator actually needs it, then unset again. Never place a real value
    # in `.env.example`.
    bootstrap_secret: str = ""

    # In-process, IP-keyed rate limiting for POST /v1/provisioning/bootstrap
    # (Phase 4D, F3) -- see app/api/rate_limit.py's RateLimiter (the same
    # primitive Phase 4C's per-API-key limits use, generalized to any
    # hashable key). Deliberately much stricter than the customer-key tiers
    # above: this endpoint is reachable by anyone who can send it a request
    # at all (no api_keys-table lookup gates it), its only real caller ever
    # needs to succeed once, and the goal is to slow down brute-forcing
    # bootstrap_secret, not to serve legitimate sustained traffic. Keyed by
    # client IP rather than any authenticated identity, since a bootstrap
    # request has none.
    bootstrap_rate_limit_capacity: int = 5
    bootstrap_rate_limit_refill_per_second: float = 0.05
    bootstrap_rate_limit_max_tracked_ips: int = 10_000

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        """Parsed, validated form of `cors_allowed_origins`.

        Raises if `"*"` is present -- wildcard CORS is not supported by
        this API at all, even if manually typed into the environment; list
        exact origins instead. Raised here (accessed once, at app-startup
        middleware configuration in app/main.py) rather than silently
        tolerated, so a dangerous misconfiguration fails loudly at process
        start instead of quietly granting every origin cross-origin access.
        """
        origins = [
            origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()
        ]
        if "*" in origins:
            raise ValueError(
                "VIGIL_API_CORS_ALLOWED_ORIGINS must not contain '*' -- list exact "
                "origins explicitly. Wildcard CORS is not supported by this API."
            )
        return origins


settings = Settings()
