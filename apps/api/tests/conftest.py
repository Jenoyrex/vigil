import os
from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

TEST_DATABASE_URL = os.environ.get(
    "VIGIL_API_TEST_DATABASE_URL",
    "postgresql+psycopg://vigil:vigil@localhost:5434/vigil_test",
)

if not TEST_DATABASE_URL.rsplit("/", 1)[-1].endswith("test"):
    raise RuntimeError(
        "VIGIL_API_TEST_DATABASE_URL does not point at a database named "
        "'*test' -- refusing to run destructive tests against it."
    )

_engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
_TestSessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)

_TABLES = "api_keys, organization_memberships, projects, organizations, users"


def _truncate_all() -> None:
    with _engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {_TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """A DB session against the dedicated vigil_test database.

    Never runs against the development database. All tables are truncated
    before and after each test, so tests are isolated and safe to run
    destructively (including tests that exercise DELETE/CASCADE behavior).
    """
    _truncate_all()
    session = _TestSessionLocal()
    try:
        yield session
    finally:
        session.close()
        _truncate_all()


class FakeSpansRepository:
    """In-memory stand-in for app.clickhouse.repository.SpansRepository.

    Records every batch it receives (so tests can assert a single batch call
    was made, not one insert per span) and can be told to raise, to exercise
    the ingestion route's ClickHouse-failure handling without a real server.
    """

    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.fail_with: Exception | None = None

    def insert_spans(self, rows: list[dict[str, Any]]) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.batches.append(rows)


@pytest.fixture
def fake_repository() -> FakeSpansRepository:
    return FakeSpansRepository()


@pytest.fixture
def client(
    db_session: Session, fake_repository: FakeSpansRepository
) -> Generator[TestClient, None, None]:
    """A TestClient wired to the test Postgres database and a fake ClickHouse
    repository -- suitable for auth/validation/transformation tests that
    should never touch a real ClickHouse server. See
    test_traces_clickhouse_integration.py for the one test that does.
    """
    from app.api.v1.traces import get_spans_repository
    from app.db.session import get_db
    from app.main import app

    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_spans_repository] = lambda: fake_repository
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def active_api_key(db_session: Session) -> SimpleNamespace:
    """An active API key for a fresh org/project, plus its raw (unhashed) value."""
    from app.security.api_keys import generate_api_key
    from test_models import make_api_key, make_organization, make_project

    org = make_organization(db_session)
    project = make_project(db_session, org)
    raw_key, key_prefix, key_hash = generate_api_key()
    api_key = make_api_key(db_session, project, key_prefix=key_prefix, key_hash=key_hash)
    return SimpleNamespace(raw_key=raw_key, organization=org, project=project, api_key=api_key)


@pytest.fixture
def revoked_api_key(db_session: Session) -> SimpleNamespace:
    from app.security.api_keys import generate_api_key
    from test_models import make_api_key, make_organization, make_project

    org = make_organization(db_session)
    project = make_project(db_session, org)
    raw_key, key_prefix, key_hash = generate_api_key()
    api_key = make_api_key(
        db_session, project, key_prefix=key_prefix, key_hash=key_hash, status="revoked"
    )
    return SimpleNamespace(raw_key=raw_key, organization=org, project=project, api_key=api_key)
