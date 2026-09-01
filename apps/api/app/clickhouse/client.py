"""ClickHouse client construction.

Connection is lazy and cached **per thread** (constructed on first use by a
given thread, reused after by that same thread): the official
`clickhouse-connect` client performs a handshake at construction time, so
eagerly building it at import time would make every Postgres-only code path
(health check, existing tests, `ruff check`) depend on ClickHouse being
reachable, and building a fresh client on every single call would reintroduce
that handshake cost on every request.

A single globally-shared instance (the original `functools.lru_cache`
version of this function) is not safe for concurrent use, though: a
`clickhouse_connect` `Client` keeps per-session query state, and using one
instance from two threads at once raises "Attempt to execute concurrent
queries within the same session. Please use a separate client instance per
thread/process." -- which FastAPI's synchronous route handlers, dispatched
to a thread pool by Starlette, do under any genuinely concurrent load (e.g.
a dashboard page firing several GET requests in parallel). Caching one
client per thread (via `threading.local`) keeps the handshake-avoidance
benefit the original caching was for while giving each thread its own
instance -- and FastAPI's dependency-override mechanism still lets tests
replace the whole client/repository without ever constructing this one.
"""

from __future__ import annotations

import threading

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from app.config import settings

_thread_local = threading.local()


def get_clickhouse_client() -> Client:
    client: Client | None = getattr(_thread_local, "client", None)
    if client is None:
        client = clickhouse_connect.get_client(
            host=settings.clickhouse_host,
            port=settings.clickhouse_port,
            database=settings.clickhouse_database,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            connect_timeout=settings.clickhouse_timeout_seconds,
            send_receive_timeout=settings.clickhouse_timeout_seconds,
        )
        _thread_local.client = client
    return client
