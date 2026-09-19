"""`POST /v1/provisioning/bootstrap` -- Phase 4D, F3. See
app/api/v1/provisioning.py and app/services/provisioning.py's module
docstrings for the full design. Covers, in order: bootstrap-token
authentication (missing/invalid/disabled/constant-time/never-a-customer-
key), the success response shape (plaintext key shown once, never
persisted, actually authenticates, correct tenant scoping), replay/
idempotency (repeated calls, concurrent calls, a failed attempt not
consuming the one-time slot), and rate limiting.

Real PostgreSQL throughout (`db_session`, per tests/conftest.py) -- no
fakes/mocks for any database invariant.
"""

from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import conftest
from app.api.rate_limit import RateLimiter, get_bootstrap_rate_limiter
from app.config import settings
from app.db.models import (
    APIKey,
    Organization,
    OrganizationMembership,
    Project,
    ProvisioningBootstrap,
    User,
)
from app.main import app
from app.services.provisioning import bootstrap_provisioning
from helpers import valid_traces_payload
from test_models import make_organization

# A dedicated sessionmaker bound to the SAME vigil_test database
# tests/conftest.py's own `db_session` fixture uses (`conftest._engine`) --
# deliberately NOT `app.db.session.SessionLocal`, which is bound to
# `settings.database_url` (the real `vigil` development database by
# default, unless an operator happens to have overridden it for this test
# run). The concurrency test below opens several independent, genuinely
# concurrent sessions from separate threads and must never risk writing to
# anything other than the disposable, truncated-per-test vigil_test
# database every other test in this suite is isolated to.
_ConcurrentTestSessionLocal: sessionmaker[Session] = sessionmaker(
    bind=conftest._engine, autoflush=False, expire_on_commit=False
)

BOOTSTRAP_URL = "/v1/provisioning/bootstrap"
BOOTSTRAP_HEADER = "X-Vigil-Bootstrap-Token"
TEST_SECRET = "test-bootstrap-secret-do-not-reuse"


@pytest.fixture(autouse=True)
def _generous_bootstrap_rate_limit() -> None:
    """`app.api.rate_limit`'s real `_bootstrap_limiter` is a process-wide
    singleton keyed by client IP, and Starlette's `TestClient` always
    reports the same simulated IP (`"testclient"`) -- every test in this
    module sharing that one real limiter (production capacity: 5) would
    exhaust it after a handful of calls and start seeing spurious 429s
    that have nothing to do with whatever that test is actually checking.
    Overridden with a large capacity here by default; only the two
    rate-limit-specific tests below deliberately re-override it again with
    a tiny one. `tests/conftest.py`'s `client` fixture clears
    `app.dependency_overrides` after every test, so this never leaks
    beyond the test it applies to.
    """
    limiter: RateLimiter[str] = RateLimiter(
        capacity=1000, refill_per_second=1000.0, max_tracked_keys=10
    )
    app.dependency_overrides[get_bootstrap_rate_limiter] = lambda: limiter


def _payload(**overrides) -> dict:
    unique = uuid.uuid4().hex[:8]
    payload = {
        "organization_name": f"Acme {unique}",
        "organization_slug": f"acme-{unique}",
        "project_name": f"Production {unique}",
        "project_slug": f"production-{unique}",
        "api_key_name": "Bootstrap key",
        "owner_email": f"owner-{unique}@example.com",
        "owner_full_name": "Ada Lovelace",
        "owner_password": "correct horse battery staple",
    }
    payload.update(overrides)
    return payload


def _with_secret(monkeypatch, secret: str = TEST_SECRET) -> None:
    monkeypatch.setattr(settings, "bootstrap_secret", secret)


# -- authentication -----------------------------------------------------


def test_valid_bootstrap_secret_succeeds(client: TestClient, monkeypatch) -> None:
    _with_secret(monkeypatch)

    response = client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: TEST_SECRET})

    assert response.status_code == 201
    body = response.json()
    assert set(body.keys()) == {
        "organization_id",
        "project_id",
        "api_key_id",
        "api_key",
        "user_id",
        "owner_email",
        "warning",
    }
    assert body["api_key"].startswith("vgl_")


def test_missing_bootstrap_token_is_401(client: TestClient, monkeypatch) -> None:
    _with_secret(monkeypatch)

    response = client.post(BOOTSTRAP_URL, json=_payload())

    assert response.status_code == 401


def test_wrong_bootstrap_secret_is_401(client: TestClient, monkeypatch) -> None:
    _with_secret(monkeypatch)

    response = client.post(
        BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: "not-the-right-secret"}
    )

    assert response.status_code == 401


def test_bootstrap_disabled_when_secret_not_configured(client: TestClient, monkeypatch) -> None:
    """The default, unconfigured state (settings.bootstrap_secret == "")
    must reject every request, including one presenting an empty token --
    the trivial "" == "" case get_bootstrap_auth's explicit up-front check
    exists specifically to close."""
    monkeypatch.setattr(settings, "bootstrap_secret", "")

    response = client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: ""})

    assert response.status_code == 401


def test_customer_api_key_cannot_authenticate_bootstrap(
    client: TestClient, active_api_key, monkeypatch
) -> None:
    """A real, active, valid customer vgl_* key must never satisfy
    get_bootstrap_auth -- presented in the bootstrap header, it is just an
    arbitrary wrong string; presented as Authorization: Bearer (its normal
    home), this endpoint never even looks at that header."""
    _with_secret(monkeypatch)

    as_bootstrap_token = client.post(
        BOOTSTRAP_URL,
        json=_payload(),
        headers={BOOTSTRAP_HEADER: active_api_key.raw_key},
    )
    assert as_bootstrap_token.status_code == 401

    as_bearer_only = client.post(
        BOOTSTRAP_URL,
        json=_payload(),
        headers={"Authorization": f"Bearer {active_api_key.raw_key}"},
    )
    assert as_bearer_only.status_code == 401


def test_bootstrap_auth_uses_constant_time_comparison(client: TestClient, monkeypatch) -> None:
    """Structural proof, not a timing measurement (flaky/unreliable in a
    test environment): asserts the actual authentication path calls
    hmac.compare_digest rather than a naive `==`, by wrapping the real
    function and recording the call."""
    _with_secret(monkeypatch)
    import app.api.deps as deps_module

    real_compare_digest = deps_module.hmac.compare_digest
    with patch.object(deps_module.hmac, "compare_digest", wraps=real_compare_digest) as spy:
        response = client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: "guess"})

    assert response.status_code == 401
    spy.assert_called_once_with("guess", TEST_SECRET)


def test_bootstrap_secret_never_appears_in_response(client: TestClient, monkeypatch) -> None:
    _with_secret(monkeypatch)

    response = client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: TEST_SECRET})

    assert TEST_SECRET not in response.text
    for header_value in response.headers.values():
        assert TEST_SECRET not in header_value


def test_bootstrap_secret_and_plaintext_key_never_appear_in_log_output(
    client: TestClient, monkeypatch
) -> None:
    """Structured-logging equivalent of the two tests above: runs every
    record emitted while handling one successful bootstrap request (a
    wrong-secret attempt first, then a real one) through the real
    `JsonFormatter` (app/logging_config.py, Phase 4D F4) -- the same
    production code path a real deployment's stdout would carry -- and
    asserts neither the configured secret, the attempted wrong guess, nor
    the newly generated plaintext API key appear anywhere in the emitted
    JSON. `api_key_prefix` (safe by design -- see
    app/security/api_keys.py) is expected to appear and is not checked
    for.
    """
    import logging

    from app.logging_config import JsonFormatter

    _with_secret(monkeypatch)

    class _CapturingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.setFormatter(JsonFormatter(service="api"))
            self.lines: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.lines.append(self.format(record))

    capture = _CapturingHandler()
    root = logging.getLogger()
    root.addHandler(capture)
    try:
        wrong_guess = "wrong-guess-should-never-be-logged"
        client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: wrong_guess})
        response = client.post(
            BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: TEST_SECRET}
        )
    finally:
        root.removeHandler(capture)

    assert response.status_code == 201
    raw_key = response.json()["api_key"]

    full_output = "\n".join(capture.lines)
    assert TEST_SECRET not in full_output
    assert wrong_guess not in full_output
    assert raw_key not in full_output


# -- success behavior: once, never persisted, actually works -----------------


def test_generated_api_key_authenticates_against_protected_endpoint(
    client: TestClient, monkeypatch
) -> None:
    _with_secret(monkeypatch)
    bootstrap_response = client.post(
        BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: TEST_SECRET}
    )
    assert bootstrap_response.status_code == 201
    raw_key = bootstrap_response.json()["api_key"]

    protected = client.get(
        "/v1/evaluations/configs", headers={"Authorization": f"Bearer {raw_key}"}
    )

    assert protected.status_code == 200


def test_plaintext_api_key_is_not_persisted(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _with_secret(monkeypatch)
    response = client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: TEST_SECRET})
    body = response.json()

    row = db_session.query(APIKey).filter(APIKey.id == uuid.UUID(body["api_key_id"])).one()

    assert row.key_hash == hashlib.sha256(body["api_key"].encode("utf-8")).hexdigest()
    # The ORM model has no plaintext-key column at all -- structural proof
    # that persistence is impossible, not just "didn't happen this time".
    assert not hasattr(row, "api_key")
    assert not hasattr(row, "raw_key")
    assert not hasattr(row, "plaintext_key")


def test_tenant_scoping_is_correct(client: TestClient, fake_repository, monkeypatch) -> None:
    """The project_id the bootstrap response names must be exactly the
    project_id the new key resolves to on a real authenticated request --
    proven by inspecting what app.services.ingestion actually wrote,
    exactly as test_traces_ingestion.py does for every other key."""
    _with_secret(monkeypatch)
    bootstrap_response = client.post(
        BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: TEST_SECRET}
    )
    body = bootstrap_response.json()

    ingest_response = client.post(
        "/v1/traces",
        json=valid_traces_payload(),
        headers={"Authorization": f"Bearer {body['api_key']}"},
    )

    assert ingest_response.status_code == 200
    [row] = fake_repository.batches[0]
    assert row["project_id"] == uuid.UUID(body["project_id"])


def test_bootstrap_creates_owner_user_with_hashed_password(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    from app.security.passwords import verify_password

    _with_secret(monkeypatch)
    payload = _payload(owner_password="a very real password 123")

    response = client.post(BOOTSTRAP_URL, json=payload, headers={BOOTSTRAP_HEADER: TEST_SECRET})

    assert response.status_code == 201
    body = response.json()

    user = db_session.query(User).filter(User.id == uuid.UUID(body["user_id"])).one()
    assert user.email == payload["owner_email"]
    assert user.full_name == payload["owner_full_name"]
    assert user.is_active is True
    assert user.hashed_password is not None
    assert user.hashed_password != payload["owner_password"]
    assert payload["owner_password"] not in user.hashed_password
    assert verify_password(payload["owner_password"], user.hashed_password) is True


def test_bootstrap_creates_owner_membership(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _with_secret(monkeypatch)

    response = client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: TEST_SECRET})

    assert response.status_code == 201
    body = response.json()

    membership = (
        db_session.query(OrganizationMembership)
        .filter(OrganizationMembership.user_id == uuid.UUID(body["user_id"]))
        .one()
    )
    assert membership.organization_id == uuid.UUID(body["organization_id"])
    assert membership.role == "owner"


def test_bootstrap_response_never_includes_the_password(
    client: TestClient, monkeypatch
) -> None:
    _with_secret(monkeypatch)
    payload = _payload(owner_password="a very unique password marker 987654")

    response = client.post(BOOTSTRAP_URL, json=payload, headers={BOOTSTRAP_HEADER: TEST_SECRET})

    assert response.status_code == 201
    assert "owner_password" not in response.json()
    assert payload["owner_password"] not in response.text


def test_bootstrap_owner_can_then_log_in(client: TestClient, monkeypatch) -> None:
    """End-to-end proof that the password bootstrap sets is the same one
    POST /v1/auth/login accepts -- not merely that a hash was stored."""
    _with_secret(monkeypatch)
    payload = _payload()

    bootstrap_response = client.post(
        BOOTSTRAP_URL, json=payload, headers={BOOTSTRAP_HEADER: TEST_SECRET}
    )
    assert bootstrap_response.status_code == 201

    login_response = client.post(
        "/v1/auth/login",
        json={"email": payload["owner_email"], "password": payload["owner_password"]},
    )
    assert login_response.status_code == 200
    assert "session_token" in login_response.json()


def test_bootstrap_password_and_hash_never_appear_in_log_output(
    client: TestClient, monkeypatch
) -> None:
    import logging

    from app.logging_config import JsonFormatter

    _with_secret(monkeypatch)
    payload = _payload(owner_password="a very unique password marker for logging 555")

    class _CapturingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.setFormatter(JsonFormatter(service="api"))
            self.lines: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.lines.append(self.format(record))

    capture = _CapturingHandler()
    root = logging.getLogger()
    root.addHandler(capture)
    try:
        response = client.post(
            BOOTSTRAP_URL, json=payload, headers={BOOTSTRAP_HEADER: TEST_SECRET}
        )
    finally:
        root.removeHandler(capture)

    assert response.status_code == 201
    full_output = "\n".join(capture.lines)
    assert payload["owner_password"] not in full_output
    # scrypt-encoded hashes always contain "scrypt$" -- a coarse but
    # effective proof no hash value leaked into a log line either.
    assert "scrypt$" not in full_output


def test_owner_password_too_short_is_422(client: TestClient, monkeypatch) -> None:
    _with_secret(monkeypatch)

    response = client.post(
        BOOTSTRAP_URL,
        json=_payload(owner_password="short"),
        headers={BOOTSTRAP_HEADER: TEST_SECRET},
    )

    assert response.status_code == 422


def test_owner_email_invalid_format_is_422(client: TestClient, monkeypatch) -> None:
    _with_secret(monkeypatch)

    response = client.post(
        BOOTSTRAP_URL,
        json=_payload(owner_email="not-an-email"),
        headers={BOOTSTRAP_HEADER: TEST_SECRET},
    )

    assert response.status_code == 422


# -- replay / idempotency / atomicity ----------------------------------------


def test_repeated_bootstrap_does_not_create_another_tenant(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _with_secret(monkeypatch)

    first = client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: TEST_SECRET})
    second = client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: TEST_SECRET})

    assert first.status_code == 201
    assert second.status_code == 409
    assert db_session.query(Organization).count() == 1
    assert db_session.query(Project).count() == 1
    assert db_session.query(APIKey).count() == 1
    assert db_session.query(ProvisioningBootstrap).count() == 1
    assert db_session.query(User).count() == 1
    assert db_session.query(OrganizationMembership).count() == 1


def test_bootstrap_already_completed_response_is_generic(client: TestClient, monkeypatch) -> None:
    _with_secret(monkeypatch)
    client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: TEST_SECRET})

    second = client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: TEST_SECRET})

    assert second.status_code == 409
    assert "already" in second.json()["detail"].lower()


def test_any_pre_existing_organization_blocks_bootstrap(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """The up-front `organizations`-non-empty check (added after review --
    see app/services/provisioning.py's module docstring) is the primary
    gate: ANY existing organization refuses bootstrap, whether or not its
    slug collides with the request, and whether or not a
    `provisioning_bootstrap` marker row exists at all. Creates no orphaned
    project/API-key/marker rows for the rejected attempt."""
    _with_secret(monkeypatch)
    existing = make_organization(db_session, slug="unrelated-org")
    existing_id = existing.id

    response = client.post(
        BOOTSTRAP_URL,
        json=_payload(),  # deliberately non-colliding slugs
        headers={BOOTSTRAP_HEADER: TEST_SECRET},
    )

    assert response.status_code == 409
    assert db_session.query(Organization).count() == 1
    assert db_session.query(Organization).one().id == existing_id
    assert db_session.query(Project).count() == 0
    assert db_session.query(APIKey).count() == 0
    assert db_session.query(ProvisioningBootstrap).count() == 0
    assert db_session.query(User).count() == 0
    assert db_session.query(OrganizationMembership).count() == 0


def test_deleting_only_the_marker_row_does_not_re_enable_bootstrap(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """The exact scenario an earlier draft of this endpoint's own
    documentation incorrectly presented as a safe reset:
    `DELETE FROM provisioning_bootstrap;` alone, leaving the organization/
    project/API key fully intact. A second bootstrap attempt afterward
    must still be refused (409) and must NOT create a second, independent
    tenant/key -- proving the invariant survives an ordinary DELETE against
    the marker table, not just concurrent HTTP requests."""
    _with_secret(monkeypatch)
    first = client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: TEST_SECRET})
    assert first.status_code == 201

    deleted = db_session.query(ProvisioningBootstrap).delete()
    db_session.commit()
    assert deleted == 1
    assert db_session.query(ProvisioningBootstrap).count() == 0
    # The organization/project/API key/user are untouched by that delete.
    assert db_session.query(Organization).count() == 1
    assert db_session.query(Project).count() == 1
    assert db_session.query(APIKey).count() == 1
    assert db_session.query(User).count() == 1

    second = client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: TEST_SECRET})

    assert second.status_code == 409
    # Still exactly one organization/project/API key/user -- the marker
    # deletion did not create a window for a second tenant to be
    # provisioned.
    assert db_session.query(Organization).count() == 1
    assert db_session.query(Project).count() == 1
    assert db_session.query(APIKey).count() == 1
    assert db_session.query(User).count() == 1


def test_integrity_error_from_a_genuine_concurrent_identical_slug_race_leaves_no_partial_state(
    db_session: Session,
) -> None:
    """The narrow race the `organizations`-non-empty check alone cannot
    close (see app/services/provisioning.py's module docstring): two
    concurrent callers both observe an empty `organizations` table before
    either commits, and both happen to choose the identical
    organization_slug. Exactly one must succeed; the other's `Organization`
    insert itself raises `IntegrityError` (the slug's unique constraint),
    which the caller must catch, roll back, and leave no partial project/
    API-key/marker row behind for -- proven here directly against real,
    independent PostgreSQL sessions, mirroring the general concurrency
    test below. `db_session` is unused directly (the race itself needs
    independent sessions -- see that test's own comment) but its fixture
    still truncates vigil_test before and after this test.
    """
    from sqlalchemy.exc import IntegrityError

    shared_slug = f"race-org-{uuid.uuid4().hex[:8]}"

    def attempt(i: int):
        session = _ConcurrentTestSessionLocal()
        try:
            try:
                return bootstrap_provisioning(
                    session,
                    organization_name="Racing Org",
                    organization_slug=shared_slug,
                    project_name=f"Racing Project {i}",
                    project_slug=f"racing-project-{i}-{uuid.uuid4().hex[:8]}",
                    api_key_name="Bootstrap key",
                    owner_email=f"racing-owner-{i}-{uuid.uuid4().hex[:8]}@example.com",
                    owner_full_name=None,
                    owner_password="correct horse battery staple",
                )
            except IntegrityError:
                session.rollback()
                return "integrity_error"
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt, i) for i in range(2)]
        results = [future.result() for future in as_completed(futures)]

    successes = [r for r in results if r is not None and r != "integrity_error"]
    assert len(successes) == 1
    assert db_session.query(Organization).count() == 1
    assert db_session.query(Project).count() == 1
    assert db_session.query(APIKey).count() == 1
    assert db_session.query(ProvisioningBootstrap).count() == 1
    assert db_session.query(User).count() == 1
    assert db_session.query(OrganizationMembership).count() == 1


def test_concurrent_bootstrap_requests_result_in_exactly_one_success(
    db_session: Session,
) -> None:
    """Exercises app.services.provisioning.bootstrap_provisioning directly
    with independent real `Session` objects (`_ConcurrentTestSessionLocal`,
    bound to vigil_test -- see that name's own comment) run from multiple
    threads simultaneously -- NOT the shared `client`/`db_session` fixture's
    single Session, which is not safe for concurrent use from multiple
    threads at once and would not actually exercise database-level
    concurrency at all. This is the real invariant
    POST /v1/provisioning/bootstrap's HTTP layer relies on.
    """

    def attempt(i: int):
        session = _ConcurrentTestSessionLocal()
        try:
            return bootstrap_provisioning(
                session,
                organization_name=f"Concurrent Org {i}",
                organization_slug=f"concurrent-org-{i}-{uuid.uuid4().hex[:8]}",
                project_name=f"Concurrent Project {i}",
                project_slug=f"concurrent-project-{i}-{uuid.uuid4().hex[:8]}",
                api_key_name="Bootstrap key",
                owner_email=f"concurrent-owner-{i}-{uuid.uuid4().hex[:8]}@example.com",
                owner_full_name=None,
                owner_password="correct horse battery staple",
            )
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(attempt, i) for i in range(8)]
        results = [future.result() for future in as_completed(futures)]

    successes = [result for result in results if result is not None]
    assert len(successes) == 1
    assert db_session.query(Organization).count() == 1
    assert db_session.query(ProvisioningBootstrap).count() == 1
    assert db_session.query(User).count() == 1
    assert db_session.query(OrganizationMembership).count() == 1


# -- rate limiting -------------------------------------------------------


def _override_bootstrap_limiter(**kwargs) -> RateLimiter:
    limiter = RateLimiter(**kwargs)
    app.dependency_overrides[get_bootstrap_rate_limiter] = lambda: limiter
    return limiter


def test_bootstrap_rate_limit_returns_429_with_retry_after(client: TestClient, monkeypatch) -> None:
    _with_secret(monkeypatch)
    _override_bootstrap_limiter(capacity=1, refill_per_second=0.0001, max_tracked_keys=10)

    first = client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: TEST_SECRET})
    second = client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: TEST_SECRET})

    assert first.status_code == 201
    assert second.status_code == 429
    assert "Retry-After" in second.headers
    assert int(second.headers["Retry-After"]) >= 1


def test_bootstrap_rate_limit_applies_even_with_wrong_secret(
    client: TestClient, monkeypatch
) -> None:
    """The rate limiter must throttle brute-force guessing itself -- it is
    listed before get_bootstrap_auth in the route's dependencies, so it
    must consume a token (and eventually 429) regardless of whether the
    guess is right or wrong."""
    _with_secret(monkeypatch)
    _override_bootstrap_limiter(capacity=1, refill_per_second=0.0001, max_tracked_keys=10)

    first = client.post(BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: "wrong-guess-1"})
    second = client.post(
        BOOTSTRAP_URL, json=_payload(), headers={BOOTSTRAP_HEADER: "wrong-guess-2"}
    )

    assert first.status_code == 401
    assert second.status_code == 429
