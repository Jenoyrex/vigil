import os
from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

# app.config.Settings.internal_service_token (Phase 3H) has no Python
# default by design (docs/decisions/005-evaluation-job-storage-worker.md
# section 9) -- it must come from the environment in every environment,
# tests included. Set before any `from app...` import anywhere in this file
# or in a collected test module triggers Settings() instantiation.
# services/worker/tests/conftest.py sets the identical value under its own
# VIGIL_WORKER_ prefix, so cross-service tests share one secret.
os.environ.setdefault("VIGIL_API_INTERNAL_SERVICE_TOKEN", "test-internal-service-token")

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

_TABLES = (
    "evaluation_jobs, evaluator_configs, evaluation_poller_checkpoint, "
    "provisioning_bootstrap, api_keys, dashboard_sessions, organization_memberships, "
    "projects, organizations, users"
)


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


class FakeTracesQueryRepository:
    """In-memory stand-in for app.clickhouse.query_repository.TracesQueryRepository.

    Records the kwargs of every call (so route-level tests can assert
    project_id/tenant scoping and parameter plumbing without a real
    ClickHouse server) and returns scripted results.
    """

    def __init__(self) -> None:
        self.list_traces_calls: list[dict[str, Any]] = []
        self.list_traces_result: list[dict[str, Any]] = []
        self.summarize_trace_calls: list[dict[str, Any]] = []
        self.summarize_trace_result: dict[str, Any] | None = None
        self.get_trace_spans_calls: list[dict[str, Any]] = []
        self.get_trace_spans_result: list[dict[str, Any]] = []
        self.get_span_calls: list[dict[str, Any]] = []
        self.get_span_result: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None

    def list_traces(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.list_traces_calls.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        return self.list_traces_result

    def summarize_trace(self, **kwargs: Any) -> dict[str, Any] | None:
        self.summarize_trace_calls.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        return self.summarize_trace_result

    def get_trace_spans(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.get_trace_spans_calls.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        return self.get_trace_spans_result

    def get_span(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.get_span_calls.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        return self.get_span_result


@pytest.fixture
def fake_traces_query_repository() -> FakeTracesQueryRepository:
    return FakeTracesQueryRepository()


class FakeAnalyticsRepository:
    """In-memory stand-in for app.clickhouse.analytics_repository.AnalyticsRepository."""

    def __init__(self) -> None:
        self.span_analytics_calls: list[dict[str, Any]] = []
        self.span_analytics_result: list[dict[str, Any]] = []
        self.llm_usage_analytics_calls: list[dict[str, Any]] = []
        self.llm_usage_analytics_result: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None

    def span_analytics(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.span_analytics_calls.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        return self.span_analytics_result

    def llm_usage_analytics(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.llm_usage_analytics_calls.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        return self.llm_usage_analytics_result


@pytest.fixture
def fake_analytics_repository() -> FakeAnalyticsRepository:
    return FakeAnalyticsRepository()


class FakeEvaluationsQueryRepository:
    """In-memory stand-in for
    app.clickhouse.evaluations_query_repository.EvaluationsQueryRepository."""

    def __init__(self) -> None:
        self.get_span_evaluations_calls: list[dict[str, Any]] = []
        self.get_span_evaluations_result: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None

    def get_span_evaluations(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.get_span_evaluations_calls.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        return self.get_span_evaluations_result


@pytest.fixture
def fake_evaluations_query_repository() -> FakeEvaluationsQueryRepository:
    return FakeEvaluationsQueryRepository()


@pytest.fixture
def client(
    db_session: Session,
    fake_repository: FakeSpansRepository,
    fake_traces_query_repository: FakeTracesQueryRepository,
    fake_analytics_repository: FakeAnalyticsRepository,
    fake_evaluations_query_repository: FakeEvaluationsQueryRepository,
) -> Generator[TestClient, None, None]:
    """A TestClient wired to the test Postgres database and fake ClickHouse
    repositories (ingestion + read-side query/analytics/evaluations) --
    suitable for auth/validation/transformation tests that should never
    touch a real ClickHouse server. See test_traces_clickhouse_integration.py,
    test_query_clickhouse_integration.py, and
    test_evaluations_clickhouse_integration.py for the tests that do.
    """
    from app.api.v1.analytics import get_analytics_repository
    from app.api.v1.evaluations import get_evaluations_query_repository
    from app.api.v1.traces import get_spans_repository, get_traces_query_repository
    from app.db.session import get_db
    from app.main import app

    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_spans_repository] = lambda: fake_repository
    app.dependency_overrides[get_traces_query_repository] = lambda: fake_traces_query_repository
    app.dependency_overrides[get_analytics_repository] = lambda: fake_analytics_repository
    app.dependency_overrides[get_evaluations_query_repository] = lambda: (
        fake_evaluations_query_repository
    )
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
def active_dashboard_user(db_session: Session) -> SimpleNamespace:
    """An active, owner-role dashboard user for a fresh org/project, plus
    the raw (unhashed) password `POST /v1/auth/login` tests authenticate
    with. Mirrors `active_api_key` above, for dashboard-user auth instead
    of customer-API-key auth -- the two are deliberately independent
    fixtures, matching the two independent authentication paths."""
    from app.db.models import OrganizationMembership
    from app.security.passwords import hash_password
    from test_models import make_organization, make_project, make_user

    org = make_organization(db_session)
    project = make_project(db_session, org)
    raw_password = "correct horse battery staple"
    user = make_user(
        db_session,
        email=f"owner-{org.id}@example.com",
        hashed_password=hash_password(raw_password),
        is_active=True,
    )
    membership = OrganizationMembership(user_id=user.id, organization_id=org.id, role="owner")
    db_session.add(membership)
    db_session.commit()

    return SimpleNamespace(
        user=user,
        raw_password=raw_password,
        organization=org,
        project=project,
        membership=membership,
    )


class FakeChResult:
    """Enough of clickhouse_connect's QueryResult interface for
    app.clickhouse.query_common.execute_query: `named_results()`."""

    def __init__(self, column_names: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
        self.column_names = column_names
        self.result_rows = rows

    def named_results(self):
        for row in self.result_rows:
            yield dict(zip(self.column_names, row, strict=True))


class FakeChQueryClient:
    """Fake clickhouse_connect Client for repository-level tests: records
    every `.query(query, parameters=...)` call (so tests can assert the
    exact generated SQL and bound parameters -- tenant scoping, FINAL
    presence/absence, etc.) and returns a scripted response.
    """

    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []
        self._responses: list[FakeChResult] = []
        self.fail_with: Exception | None = None

    def queue_result(self, column_names: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
        self._responses.append(FakeChResult(column_names, rows))

    def query(self, query: str, parameters: dict[str, Any] | None = None, **_: Any) -> FakeChResult:
        self.calls.append(SimpleNamespace(query=query, parameters=parameters or {}))
        if self.fail_with is not None:
            raise self.fail_with
        if self._responses:
            return self._responses.pop(0)
        return FakeChResult((), [])

    @property
    def last_query(self) -> str:
        return self.calls[-1].query

    @property
    def last_parameters(self) -> dict[str, Any]:
        return self.calls[-1].parameters


@pytest.fixture
def fake_ch_query_client() -> FakeChQueryClient:
    return FakeChQueryClient()


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
