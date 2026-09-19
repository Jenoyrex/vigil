"""Tests for GET /ready (Phase 4A: PostgreSQL readiness).

`get_clickhouse_client`/`ping_database` are patched where `app.main` looks
them up (both are imported by name into that module), not where they're
defined -- the standard `unittest.mock.patch` convention for this shape.
No real ClickHouse/PostgreSQL server is needed for any test here.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_ready_returns_ok_when_both_stores_are_reachable() -> None:
    fake_client = MagicMock()
    fake_client.ping.return_value = None
    with (
        patch("app.main.get_clickhouse_client", return_value=fake_client),
        patch("app.main.ping_database", return_value=None),
    ):
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "clickhouse": "ok", "postgresql": "ok"}


def test_ready_returns_503_when_clickhouse_is_unreachable() -> None:
    fake_client = MagicMock()
    fake_client.ping.side_effect = RuntimeError("connection refused: 10.0.0.1:8123")
    with (
        patch("app.main.get_clickhouse_client", return_value=fake_client),
        patch("app.main.ping_database") as fake_ping_database,
    ):
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "ClickHouse is unreachable."}
    # ClickHouse is checked first; a ClickHouse failure must short-circuit
    # before PostgreSQL is ever checked.
    fake_ping_database.assert_not_called()


def test_ready_returns_503_when_postgresql_is_unreachable() -> None:
    fake_client = MagicMock()
    fake_client.ping.return_value = None
    with (
        patch("app.main.get_clickhouse_client", return_value=fake_client),
        patch(
            "app.main.ping_database", side_effect=RuntimeError("connection refused: 10.0.0.2:5432")
        ),
    ):
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "PostgreSQL is unreachable."}


def test_ready_never_leaks_the_raw_driver_exception_message() -> None:
    fake_client = MagicMock()
    fake_client.ping.return_value = None
    with (
        patch("app.main.get_clickhouse_client", return_value=fake_client),
        patch(
            "app.main.ping_database",
            side_effect=RuntimeError("password authentication failed for user 'vigil_prod_admin'"),
        ),
    ):
        response = client.get("/ready")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail == "PostgreSQL is unreachable."
    assert "vigil_prod_admin" not in detail


def test_ready_ping_database_raises_on_a_real_unreachable_engine() -> None:
    """Unit test of `ping_database` itself against a real (but deliberately
    unreachable) SQLAlchemy engine -- no mocking, no real server required:
    connection refusal alone is enough to prove this raises rather than
    hangs. A short connect_timeout keeps this fast."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError

    from app.db.session import ping_database as real_ping_database

    unreachable_engine = create_engine(
        "postgresql+psycopg://nobody:nobody@127.0.0.1:1/nonexistent",
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )

    with patch("app.db.session.engine", unreachable_engine):
        try:
            real_ping_database()
        except OperationalError:
            pass
        else:
            raise AssertionError("expected ping_database() to raise for an unreachable engine")


def test_health_is_unaffected_by_the_readiness_change() -> None:
    """GET /health must remain a pure liveness check with zero backing-store
    dependency -- regression guard against this Phase 4A change ever
    touching it."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
