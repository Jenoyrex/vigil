"""ClickHouse client construction for services/worker.

Deliberately its own implementation, not an import of
`apps/api/app/clickhouse/client.py` -- services/worker must not depend on
apps/api, per ADR 001 decision 6's "duplicate rather than centralize" (the
same reasoning docs/decisions/005-evaluation-job-storage-worker.md section 6
already applies to the worker's PostgreSQL access). The caching strategy is
identical to apps/api's for the identical reason: `clickhouse_connect`
performs a handshake at construction time, and a `Client` instance is not
safe to share across concurrent callers, so each thread gets its own
lazily-built, cached instance.
"""

from __future__ import annotations

import threading

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from worker.config import settings

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
