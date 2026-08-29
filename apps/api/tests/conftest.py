import os
from collections.abc import Generator

import pytest
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
