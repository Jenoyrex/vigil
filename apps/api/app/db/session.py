from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    # PostgreSQL I/O bound (Phase 4D) -- mirrors services/worker/worker/
    # postgres/client.py's identical connect_timeout/statement_timeout pair
    # via the same settings.database_timeout_seconds knob (see
    # app/config.py). connect_timeout is libpq's own connection-establishment
    # bound; the `options` value sets PostgreSQL's statement_timeout as a
    # session GUC at connect time, which -- because it isn't transaction-
    # scoped -- stays in effect for a pooled connection's entire lifetime,
    # not just its first checkout. Without this, a stuck query (or even
    # pool_pre_ping's own liveness SELECT) could block a request thread
    # indefinitely.
    connect_args={
        "connect_timeout": int(settings.database_timeout_seconds),
        "options": f"-c statement_timeout={int(settings.database_timeout_seconds * 1000)}",
    },
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ping_database() -> None:
    """Cheap PostgreSQL connectivity check for `GET /ready` (Phase 4A) --
    a single `SELECT 1` against a pooled connection, mirroring `GET /ready`'s
    existing `get_clickhouse_client().ping()` check in both cost and
    convention. Deliberately not routed through `get_db`'s request-scoped
    `Session` machinery, which exists for route handlers, not a liveness
    probe. `engine`'s own `pool_pre_ping=True` already validates a
    checked-out pooled connection as part of this call; on a cold pool this
    opens one fresh connection -- the same order-of-magnitude cost as the
    ClickHouse check it runs alongside.

    Raises on failure (connection refused, auth failure, timeout); the
    caller (`app.main.ready`) maps that to a 503, never leaking the raw
    driver exception to the client.
    """
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
