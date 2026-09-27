"""Self-serve signup + workspace routes (app/api/v1/workspace.py) and the
session-scoped project access every read/config route now accepts
(app.api.deps.get_project_access) -- above all, that one tenant can never
reach another tenant's projects, keys, traces or evaluator configs by
swapping an id.

Real PostgreSQL throughout (`client`/`db_session`, tests/conftest.py).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.rate_limit import (
    RateLimiter,
    get_default_rate_limiter,
    get_login_account_rate_limiter,
    get_login_rate_limiter,
)
from app.main import app

SESSION = "X-Vigil-Session-Token"
PROJECT = "X-Vigil-Project-Id"
PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _generous_rate_limits() -> None:
    """Process-wide limiters shared across tests would otherwise 429; see
    test_auth_api.py's identical fixture."""
    for dep in (get_login_rate_limiter, get_login_account_rate_limiter, get_default_rate_limiter):
        app.dependency_overrides[dep] = _returning(
            RateLimiter(capacity=1000, refill_per_second=1000.0, max_tracked_keys=1000)
        )


def _returning(limiter: RateLimiter):  # noqa: ANN202
    return lambda: limiter


def _signup(client: TestClient, email: str) -> dict[str, str]:
    response = client.post("/v1/auth/signup", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return {SESSION: response.json()["session_token"]}


def _tenant(client: TestClient, email: str) -> tuple[dict[str, str], str, str]:
    """Sign up, create an org and a project. Returns (auth headers, org id, project id)."""
    headers = _signup(client, email)
    org = client.post("/v1/organizations", json={"name": "Acme AI"}, headers=headers)
    assert org.status_code == 201, org.text
    project = client.post(
        f"/v1/organizations/{org.json()['id']}/projects", json={"name": "Chatbot"}, headers=headers
    )
    assert project.status_code == 201, project.text
    return headers, org.json()["id"], project.json()["id"]


# -- signup ------------------------------------------------------------------


def test_signup_creates_session_and_user_can_log_in(client: TestClient) -> None:
    headers = _signup(client, "New.User@Example.com")
    session = client.get("/v1/auth/session", headers=headers)
    assert session.status_code == 200
    assert session.json()["email"] == "new.user@example.com"

    login = client.post(
        "/v1/auth/login", json={"email": "new.user@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200


def test_signup_duplicate_email_is_409_case_insensitively(client: TestClient) -> None:
    _signup(client, "dup@example.com")
    response = client.post(
        "/v1/auth/signup", json={"email": "DUP@example.com", "password": PASSWORD}
    )
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"email": "not-an-email", "password": PASSWORD},
        {"email": "short@example.com", "password": "short"},
        {"email": "missing@example.com"},
    ],
)
def test_signup_validation(client: TestClient, body: dict[str, str]) -> None:
    assert client.post("/v1/auth/signup", json=body).status_code == 422


def test_signup_never_stores_plaintext_password(client: TestClient, db_session) -> None:
    from app.db.models import User

    _signup(client, "hash@example.com")
    user = db_session.query(User).filter(User.email == "hash@example.com").one()
    assert user.hashed_password.startswith("scrypt$")
    assert PASSWORD not in user.hashed_password


# -- workspace ---------------------------------------------------------------


def test_new_user_has_no_organizations_until_onboarding(client: TestClient) -> None:
    headers = _signup(client, "fresh@example.com")
    me = client.get("/v1/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["organizations"] == []


def test_onboarding_makes_creator_owner_and_lists_project(client: TestClient) -> None:
    headers, org_id, project_id = _tenant(client, "owner@example.com")
    orgs = client.get("/v1/me", headers=headers).json()["organizations"]
    assert [(o["id"], o["role"]) for o in orgs] == [(org_id, "owner")]
    assert [p["id"] for p in orgs[0]["projects"]] == [project_id]


def test_names_are_trimmed_and_blank_names_rejected(client: TestClient) -> None:
    headers = _signup(client, "names@example.com")
    blank = client.post("/v1/organizations", json={"name": "   "}, headers=headers)
    assert blank.status_code == 422
    org = client.post("/v1/organizations", json={"name": "  Acme  "}, headers=headers)
    assert org.json()["name"] == "Acme"


def test_workspace_routes_require_a_session(client: TestClient, active_api_key) -> None:
    assert client.get("/v1/me").status_code == 401
    assert client.post("/v1/organizations", json={"name": "x"}).status_code == 401
    # A customer API key is not a session.
    bearer = {"Authorization": f"Bearer {active_api_key.raw_key}"}
    assert client.get("/v1/me", headers=bearer).status_code == 401


def test_api_key_lifecycle(client: TestClient) -> None:
    headers, _, project_id = _tenant(client, "keys@example.com")
    created = client.post(
        f"/v1/projects/{project_id}/api-keys", json={"name": "prod"}, headers=headers
    )
    assert created.status_code == 201
    body = created.json()
    raw_key = body["api_key"]
    assert raw_key.startswith(body["key_prefix"] + ".")
    assert "key_hash" not in body

    listed = client.get(f"/v1/projects/{project_id}/api-keys", headers=headers).json()["items"]
    assert [k["id"] for k in listed] == [body["id"]]
    assert "api_key" not in listed[0] and "key_hash" not in listed[0]

    # The new key really authenticates ingestion for this project...
    bearer = {"Authorization": f"Bearer {raw_key}"}
    assert client.get("/v1/evaluations/configs", headers=bearer).status_code == 200

    # ...until revoked.
    revoked = client.post(
        f"/v1/projects/{project_id}/api-keys/{body['id']}/revoke", headers=headers
    )
    assert revoked.json()["status"] == "revoked"
    assert client.get("/v1/evaluations/configs", headers=bearer).status_code == 401


# -- session-scoped project access -------------------------------------------


def test_session_reads_own_project(client: TestClient, fake_traces_query_repository) -> None:
    headers, _, project_id = _tenant(client, "reader@example.com")
    response = client.get("/v1/traces", headers={**headers, PROJECT: project_id})
    assert response.status_code == 200
    assert str(fake_traces_query_repository.list_traces_calls[-1]["project_id"]) == project_id


def test_session_without_project_header_is_404(client: TestClient) -> None:
    headers, _, _ = _tenant(client, "nohdr@example.com")
    assert client.get("/v1/traces", headers=headers).status_code == 404


def test_ingestion_still_requires_an_api_key(client: TestClient) -> None:
    headers, _, project_id = _tenant(client, "ingest@example.com")
    response = client.post(
        "/v1/traces", json={"spans": []}, headers={**headers, PROJECT: project_id}
    )
    assert response.status_code == 401


def test_cross_tenant_access_is_denied_everywhere(
    client: TestClient, fake_traces_query_repository
) -> None:
    alice, alice_org, alice_project = _tenant(client, "alice@example.com")
    bob, _, bob_project = _tenant(client, "bob@example.com")
    key = client.post(
        f"/v1/projects/{alice_project}/api-keys", json={"name": "a"}, headers=alice
    ).json()

    as_bob_on_alice = {**bob, PROJECT: alice_project}
    for path in (
        "/v1/traces",
        f"/v1/traces/{'a' * 32}",
        "/v1/analytics/spans",
        "/v1/evaluations/configs",
        "/v1/evaluations/jobs",
    ):
        assert client.get(path, headers=as_bob_on_alice).status_code == 404, path
    assert (
        client.put(
            "/v1/evaluations/configs/relevance", json={"enabled": True}, headers=as_bob_on_alice
        ).status_code
        == 404
    )
    assert client.get(f"/v1/projects/{alice_project}/api-keys", headers=bob).status_code == 404
    assert (
        client.post(
            f"/v1/projects/{alice_project}/api-keys", json={"name": "steal"}, headers=bob
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/projects/{alice_project}/api-keys/{key['id']}/revoke", headers=bob
        ).status_code
        == 404
    )
    # Bob can't revoke Alice's key through his own project either.
    assert (
        client.post(
            f"/v1/projects/{bob_project}/api-keys/{key['id']}/revoke", headers=bob
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/organizations/{alice_org}/projects", json={"name": "x"}, headers=bob
        ).status_code
        == 404
    )
    # A random project id looks exactly like someone else's.
    random_project = {**bob, PROJECT: str(uuid.uuid4())}
    assert client.get("/v1/traces", headers=random_project).status_code == 404
    # Nothing ever reached ClickHouse on Bob's behalf for Alice's project.
    assert all(
        str(call["project_id"]) != alice_project
        for call in fake_traces_query_repository.list_traces_calls
    )
    # Alice's key is still active.
    listed = client.get(f"/v1/projects/{alice_project}/api-keys", headers=alice).json()["items"]
    assert listed[0]["status"] == "active"


def test_member_role_cannot_manage(client: TestClient, db_session) -> None:
    from app.db.models import OrganizationMembership, User

    _, org_id, project_id = _tenant(client, "boss@example.com")
    member = _signup(client, "member@example.com")
    user = db_session.query(User).filter(User.email == "member@example.com").one()
    db_session.add(
        OrganizationMembership(user_id=user.id, organization_id=uuid.UUID(org_id), role="member")
    )
    db_session.commit()

    assert client.get(f"/v1/projects/{project_id}/api-keys", headers=member).status_code == 200
    assert (
        client.post(
            f"/v1/projects/{project_id}/api-keys", json={"name": "x"}, headers=member
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/organizations/{org_id}/projects", json={"name": "x"}, headers=member
        ).status_code
        == 403
    )
