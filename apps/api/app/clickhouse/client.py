"""ClickHouse client construction.

Connection is lazy and cached (constructed on first use, reused after): the
official `clickhouse-connect` client performs a handshake at construction
time, so eagerly building it at import time would make every Postgres-only
code path (health check, existing tests, `ruff check`) depend on ClickHouse
being reachable. Building it lazily means nothing touches ClickHouse until a
request actually needs it -- and FastAPI's dependency-override mechanism lets
tests replace the whole client/repository without ever constructing this one.
"""

from __future__ import annotations

from functools import lru_cache

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from app.config import settings


@lru_cache(maxsize=1)
def get_clickhouse_client() -> Client:
    return clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        database=settings.clickhouse_database,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        connect_timeout=settings.clickhouse_timeout_seconds,
        send_receive_timeout=settings.clickhouse_timeout_seconds,
    )
