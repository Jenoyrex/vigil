"""API tests for the evaluations surface.

`POST /v1/evaluations/jobs` (ADR 005 sections 9/10, Phase 3H amendment) is
internal-token-only. Every other test class in this file is Phase 3I
(customer-facing: `evaluator_configs` CRUD, evaluation-job status list,
span-scoped evaluation-results read) -- `Authorization: Bearer`, never
`X-Vigil-Internal-Token`.

Uses the `client` fixture (real Postgres test DB, fake
TracesQueryRepository/EvaluationsQueryRepository) exactly like
test_traces_api.py-style tests already do for the customer-facing endpoints
-- the relevant `get_*_repository` dependency is imported and reused
directly from its owning route module (not redefined) so the SAME fixture
overrides already wired in tests/conftest.py's `client` fixture cover these
routes too.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models import EvaluationJob, EvaluatorConfig
from test_models import (
    make_evaluation_job,
    make_evaluator_config,
    make_organization,
    make_project,
)

INTERNAL_TOKEN_HEADER = "X-Vigil-Internal-Token"
VALID_INTERNAL_TOKEN = "test-internal-service-token"  # matches conftest.py's os.environ.setdefault

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"


def _auth_headers(active_api_key) -> dict[str, str]:
    return {"Authorization": f"Bearer {active_api_key.raw_key}"}


def _payload(*, project_id: uuid.UUID, **overrides) -> dict:
    defaults = {
        "project_id": str(project_id),
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "evaluator_name": "relevance",
        "evaluator_version": "0.1.0",
    }
    return {**defaults, **overrides}


def _make_project(db_session: Session):
    org = make_organization(db_session)
    return make_project(db_session, org)


def _mark_span_found(fake_traces_query_repository) -> None:
    fake_traces_query_repository.get_span_result = [{"span_id": SPAN_ID}]


# -- authentication -----------------------------------------------------------


def test_missing_internal_token_is_401(client) -> None:
    # project_id doesn't matter -- auth is checked before the body is even read.
    response = client.post(
        "/v1/evaluations/jobs",
        json=_payload(project_id=uuid.uuid4()),
    )
    assert response.status_code == 401


def test_wrong_internal_token_is_401(client) -> None:
    response = client.post(
        "/v1/evaluations/jobs",
        json=_payload(project_id=uuid.uuid4()),
        headers={INTERNAL_TOKEN_HEADER: "not-the-right-token"},
    )
    assert response.status_code == 401


def test_customer_api_key_does_not_authenticate_this_endpoint(client, active_api_key) -> None:
    """A valid, active customer vgl_* key -- presented the customer way,
    Authorization: Bearer -- must not satisfy get_internal_service_auth.
    This endpoint doesn't even look at that header."""
    response = client.post(
        "/v1/evaluations/jobs",
        json=_payload(project_id=active_api_key.project.id),
        headers={"Authorization": f"Bearer {active_api_key.raw_key}"},
    )
    assert response.status_code == 401


def test_valid_internal_token_authenticates(
    client, db_session: Session, fake_traces_query_repository
) -> None:
    project = _make_project(db_session)
    make_evaluator_config(db_session, project, enabled=True, sampling_rate=1.0)
    _mark_span_found(fake_traces_query_repository)

    response = client.post(
        "/v1/evaluations/jobs",
        json=_payload(project_id=project.id),
        headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN},
    )
    assert response.status_code == 201


# -- validation -----------------------------------------------------------


def test_malformed_trace_id_is_422(client) -> None:
    response = client.post(
        "/v1/evaluations/jobs",
        json=_payload(project_id=uuid.uuid4(), trace_id="not-valid-hex"),
        headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN},
    )
    assert response.status_code == 422


def test_malformed_span_id_is_422(client) -> None:
    response = client.post(
        "/v1/evaluations/jobs",
        json=_payload(project_id=uuid.uuid4(), span_id="too-short"),
        headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN},
    )
    assert response.status_code == 422


def test_missing_evaluator_name_is_422(client) -> None:
    payload = _payload(project_id=uuid.uuid4())
    del payload["evaluator_name"]
    response = client.post(
        "/v1/evaluations/jobs", json=payload, headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN}
    )
    assert response.status_code == 422


# -- eligibility outcomes ------------------------------------------------------


def test_not_enabled_when_no_config_exists(
    client, db_session: Session, fake_traces_query_repository
) -> None:
    project = _make_project(db_session)
    _mark_span_found(fake_traces_query_repository)

    response = client.post(
        "/v1/evaluations/jobs",
        json=_payload(project_id=project.id),
        headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"job_id": None, "created": False, "reason": "not_enabled"}


def test_not_enabled_when_config_exists_but_disabled(
    client, db_session: Session, fake_traces_query_repository
) -> None:
    project = _make_project(db_session)
    make_evaluator_config(db_session, project, enabled=False)
    _mark_span_found(fake_traces_query_repository)

    response = client.post(
        "/v1/evaluations/jobs",
        json=_payload(project_id=project.id),
        headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN},
    )
    assert response.status_code == 200
    assert response.json()["reason"] == "not_enabled"


def test_not_sampled_when_sampling_rate_is_zero(
    client, db_session: Session, fake_traces_query_repository
) -> None:
    project = _make_project(db_session)
    make_evaluator_config(db_session, project, enabled=True, sampling_rate=0.0)
    _mark_span_found(fake_traces_query_repository)

    response = client.post(
        "/v1/evaluations/jobs",
        json=_payload(project_id=project.id),
        headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"job_id": None, "created": False, "reason": "not_sampled"}


def test_404_when_span_not_found(client, db_session: Session, fake_traces_query_repository) -> None:
    project = _make_project(db_session)
    make_evaluator_config(db_session, project, enabled=True, sampling_rate=1.0)
    fake_traces_query_repository.get_span_result = []  # not found

    response = client.post(
        "/v1/evaluations/jobs",
        json=_payload(project_id=project.id),
        headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN},
    )
    assert response.status_code == 404


def test_404_when_span_belongs_to_a_different_project(
    client, db_session: Session, fake_traces_query_repository
) -> None:
    """get_span already scopes its own WHERE by project_id -- a span
    belonging to a different project is indistinguishable, from this
    endpoint's perspective, from a genuinely missing one. Both must 404.
    """
    project = _make_project(db_session)
    make_evaluator_config(db_session, project, enabled=True, sampling_rate=1.0)
    # Simulates the wrong-project case: get_span, scoped by the asserted
    # project_id, finds nothing, exactly as if the span didn't exist.
    fake_traces_query_repository.get_span_result = []

    response = client.post(
        "/v1/evaluations/jobs",
        json=_payload(project_id=project.id),
        headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN},
    )
    assert response.status_code == 404
    assert len(fake_traces_query_repository.get_span_calls) == 1
    assert fake_traces_query_repository.get_span_calls[0]["project_id"] == project.id


# -- creation / idempotency ----------------------------------------------------


def test_new_job_is_201_created(client, db_session: Session, fake_traces_query_repository) -> None:
    project = _make_project(db_session)
    make_evaluator_config(db_session, project, enabled=True, sampling_rate=1.0)
    _mark_span_found(fake_traces_query_repository)

    response = client.post(
        "/v1/evaluations/jobs",
        json=_payload(project_id=project.id),
        headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True
    assert body["reason"] == "created"
    assert body["job_id"] is not None


def test_duplicate_job_is_200_already_exists_with_same_job_id(
    client, db_session: Session, fake_traces_query_repository
) -> None:
    project = _make_project(db_session)
    make_evaluator_config(db_session, project, enabled=True, sampling_rate=1.0)
    _mark_span_found(fake_traces_query_repository)
    payload = _payload(project_id=project.id)

    first = client.post(
        "/v1/evaluations/jobs", json=payload, headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN}
    )
    second = client.post(
        "/v1/evaluations/jobs", json=payload, headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN}
    )

    assert first.status_code == 201
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["created"] is False
    assert second_body["reason"] == "already_exists"
    assert second_body["job_id"] == first.json()["job_id"]

    rows = db_session.query(EvaluationJob).filter(EvaluationJob.project_id == project.id).all()
    assert len(rows) == 1  # never duplicated


def test_max_retries_is_snapshotted_from_evaluator_config(
    client, db_session: Session, fake_traces_query_repository
) -> None:
    project = _make_project(db_session)
    make_evaluator_config(db_session, project, enabled=True, sampling_rate=1.0, max_retries=7)
    _mark_span_found(fake_traces_query_repository)

    response = client.post(
        "/v1/evaluations/jobs",
        json=_payload(project_id=project.id),
        headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN},
    )
    assert response.status_code == 201

    job = db_session.get(EvaluationJob, uuid.UUID(response.json()["job_id"]))
    assert job.max_retries == 7


def test_evaluator_version_is_snapshotted_from_request(
    client, db_session: Session, fake_traces_query_repository
) -> None:
    project = _make_project(db_session)
    make_evaluator_config(db_session, project, enabled=True, sampling_rate=1.0)
    _mark_span_found(fake_traces_query_repository)

    response = client.post(
        "/v1/evaluations/jobs",
        json=_payload(project_id=project.id, evaluator_version="9.9.9"),
        headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN},
    )
    assert response.status_code == 201

    job = db_session.get(EvaluationJob, uuid.UUID(response.json()["job_id"]))
    assert job.evaluator_version == "9.9.9"


def test_threshold_is_never_snapshotted(
    client, db_session: Session, fake_traces_query_repository
) -> None:
    """evaluation_jobs has no threshold column at all -- threshold
    continues to resolve at execution time (Phase 3A/3C), unchanged."""
    project = _make_project(db_session)
    make_evaluator_config(db_session, project, enabled=True, sampling_rate=1.0, threshold=0.42)
    _mark_span_found(fake_traces_query_repository)

    response = client.post(
        "/v1/evaluations/jobs",
        json=_payload(project_id=project.id),
        headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN},
    )
    assert response.status_code == 201
    assert not hasattr(EvaluationJob, "threshold")


# =============================================================================
# Phase 3I: customer-facing evaluations API (get_current_api_key, never
# X-Vigil-Internal-Token)
# =============================================================================


# -- evaluator_configs CRUD ---------------------------------------------------


def test_list_configs_requires_customer_auth(client) -> None:
    response = client.get("/v1/evaluations/configs")
    assert response.status_code == 401


def test_list_configs_internal_token_does_not_authenticate(client, active_api_key) -> None:
    """The internal worker token must not satisfy a customer-facing route --
    structurally separate boundaries, proven both directions (the reverse
    is already proven by test_customer_api_key_does_not_authenticate_this_endpoint
    above, for POST /v1/evaluations/jobs)."""
    response = client.get(
        "/v1/evaluations/configs", headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN}
    )
    assert response.status_code == 401


def test_list_configs_empty_when_none_configured(client, active_api_key) -> None:
    response = client.get("/v1/evaluations/configs", headers=_auth_headers(active_api_key))
    assert response.status_code == 200
    assert response.json() == {"configs": []}


def test_list_configs_returns_configured_evaluators(
    client, db_session: Session, active_api_key
) -> None:
    make_evaluator_config(db_session, active_api_key.project, evaluator_name="relevance")
    make_evaluator_config(db_session, active_api_key.project, evaluator_name="relevance_embedding")

    response = client.get("/v1/evaluations/configs", headers=_auth_headers(active_api_key))
    assert response.status_code == 200
    names = {config["evaluator_name"] for config in response.json()["configs"]}
    assert names == {"relevance", "relevance_embedding"}


def test_list_configs_scoped_to_project(client, db_session: Session, active_api_key) -> None:
    other_project = _make_project(db_session)
    make_evaluator_config(db_session, other_project, evaluator_name="relevance")

    response = client.get("/v1/evaluations/configs", headers=_auth_headers(active_api_key))
    assert response.status_code == 200
    assert response.json() == {"configs": []}


def test_get_config_404_when_not_configured(client, active_api_key) -> None:
    response = client.get(
        "/v1/evaluations/configs/relevance", headers=_auth_headers(active_api_key)
    )
    assert response.status_code == 404


def test_get_config_returns_existing(client, db_session: Session, active_api_key) -> None:
    make_evaluator_config(
        db_session,
        active_api_key.project,
        evaluator_name="relevance",
        enabled=True,
        sampling_rate=0.5,
        threshold=0.42,
        max_retries=5,
    )

    response = client.get(
        "/v1/evaluations/configs/relevance", headers=_auth_headers(active_api_key)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["evaluator_name"] == "relevance"
    assert body["enabled"] is True
    assert body["sampling_rate"] == 0.5
    assert body["threshold"] == 0.42
    assert body["max_retries"] == 5


def test_get_config_scoped_to_project(client, db_session: Session, active_api_key) -> None:
    """A config that exists, but for a different project, must 404 -- not
    leak another tenant's configuration."""
    other_project = _make_project(db_session)
    make_evaluator_config(db_session, other_project, evaluator_name="relevance")

    response = client.get(
        "/v1/evaluations/configs/relevance", headers=_auth_headers(active_api_key)
    )
    assert response.status_code == 404


def test_put_config_creates_new(client, active_api_key) -> None:
    response = client.put(
        "/v1/evaluations/configs/relevance",
        json={"enabled": True, "sampling_rate": 0.25, "max_retries": 4},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["evaluator_name"] == "relevance"
    assert body["enabled"] is True
    assert body["sampling_rate"] == 0.25
    assert body["threshold"] is None
    assert body["max_retries"] == 4


def test_put_config_omitted_fields_use_documented_defaults(client, active_api_key) -> None:
    """Full-replace (PUT) semantics: omitting sampling_rate/threshold/
    max_retries resolves each to its own documented default -- the same
    defaults evaluator_configs' own DB schema uses -- not an error."""
    response = client.put(
        "/v1/evaluations/configs/relevance",
        json={"enabled": True},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sampling_rate"] == 0.1
    assert body["threshold"] is None
    assert body["max_retries"] == 3


def test_put_config_is_idempotent(client, active_api_key) -> None:
    payload = {"enabled": True, "sampling_rate": 0.5, "max_retries": 2}
    first = client.put(
        "/v1/evaluations/configs/relevance", json=payload, headers=_auth_headers(active_api_key)
    )
    second = client.put(
        "/v1/evaluations/configs/relevance", json=payload, headers=_auth_headers(active_api_key)
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["created_at"] == second.json()["created_at"]


def test_put_config_updates_existing_without_duplicating(
    client, db_session: Session, active_api_key
) -> None:
    make_evaluator_config(
        db_session, active_api_key.project, evaluator_name="relevance", enabled=False
    )

    response = client.put(
        "/v1/evaluations/configs/relevance",
        json={"enabled": True, "sampling_rate": 0.9, "max_retries": 1},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["sampling_rate"] == 0.9

    rows = (
        db_session.query(EvaluatorConfig)
        .filter(EvaluatorConfig.project_id == active_api_key.project.id)
        .all()
    )
    assert len(rows) == 1  # never duplicated


def test_put_config_omitting_a_previously_set_field_resets_it_to_default(
    client, db_session: Session, active_api_key
) -> None:
    """PUT is full-replace, not partial patch: a prior custom sampling_rate
    must NOT survive a later PUT that omits it."""
    make_evaluator_config(
        db_session, active_api_key.project, evaluator_name="relevance", sampling_rate=0.77
    )

    response = client.put(
        "/v1/evaluations/configs/relevance",
        json={"enabled": True},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 200
    assert response.json()["sampling_rate"] == 0.1


def test_put_config_sampling_rate_above_one_is_422(client, active_api_key) -> None:
    response = client.put(
        "/v1/evaluations/configs/relevance",
        json={"enabled": True, "sampling_rate": 1.5},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 422


def test_put_config_negative_max_retries_is_422(client, active_api_key) -> None:
    response = client.put(
        "/v1/evaluations/configs/relevance",
        json={"enabled": True, "max_retries": -1},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 422


def test_put_config_missing_enabled_is_422(client, active_api_key) -> None:
    """enabled has no default -- this endpoint IS the enable/disable
    mechanism, so every call must state it explicitly."""
    response = client.put(
        "/v1/evaluations/configs/relevance",
        json={"sampling_rate": 0.5},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 422


def test_put_config_does_not_affect_another_project(
    client, db_session: Session, active_api_key
) -> None:
    other_project = _make_project(db_session)

    response = client.put(
        "/v1/evaluations/configs/relevance",
        json={"enabled": True},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 200

    other_config = (
        db_session.query(EvaluatorConfig)
        .filter(EvaluatorConfig.project_id == other_project.id)
        .first()
    )
    assert other_config is None


# -- evaluator_name validation (Phase 4A) -------------------------------------
#
# `EvaluatorName` (app/schemas/evaluations.py) bounds length (1-128) and
# character set ([A-Za-z0-9_.-]) on every customer-facing evaluator_name --
# GET/PUT .../configs/{evaluator_name} and the GET .../jobs?evaluator_name=
# filter. Deliberately does NOT enumerate specific names: "relevance" and
# "relevance_embedding" must keep working precisely because nothing here
# hardcodes them, and an unrecognized-but-well-formed name must still be
# accepted as valid input (ADR 005 section 2's open-string design).

_VALID_LENGTH_NAME = "e" * 128
_OVERSIZED_NAME = "e" * 129


def test_get_config_128_char_evaluator_name_is_not_a_422(client, active_api_key) -> None:
    response = client.get(
        f"/v1/evaluations/configs/{_VALID_LENGTH_NAME}", headers=_auth_headers(active_api_key)
    )
    # Well-formed but never configured -- 404, not 422: validation must
    # accept this input and let the existing "never configured" semantics
    # decide the outcome.
    assert response.status_code == 404


def test_get_config_oversized_evaluator_name_is_422(client, active_api_key) -> None:
    response = client.get(
        f"/v1/evaluations/configs/{_OVERSIZED_NAME}", headers=_auth_headers(active_api_key)
    )
    assert response.status_code == 422


def test_put_config_128_char_evaluator_name_succeeds(client, active_api_key) -> None:
    response = client.put(
        f"/v1/evaluations/configs/{_VALID_LENGTH_NAME}",
        json={"enabled": True},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 200
    assert response.json()["evaluator_name"] == _VALID_LENGTH_NAME


def test_put_config_oversized_evaluator_name_is_422(client, active_api_key) -> None:
    response = client.put(
        f"/v1/evaluations/configs/{_OVERSIZED_NAME}",
        json={"enabled": True},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 422


def test_put_config_evaluator_name_with_invalid_characters_is_422(client, active_api_key) -> None:
    # Each of these stays within a single URL path segment (no raw `/`, `?`,
    # or `#`, which would change how the URL itself parses rather than
    # exercise the validator) but contains a character outside
    # [A-Za-z0-9_.-]. `%20` is an explicit, unambiguous percent-encoded
    # space -- decodes server-side to "has space" regardless of the test
    # client's own auto-encoding behavior for a literal space.
    for invalid_path_segment in ("has!bang", "has@at", "has$dollar", "has%20space"):
        response = client.put(
            f"/v1/evaluations/configs/{invalid_path_segment}",
            json={"enabled": True},
            headers=_auth_headers(active_api_key),
        )
        assert response.status_code == 422, f"expected 422 for {invalid_path_segment!r}"


def test_list_jobs_filter_oversized_evaluator_name_is_422(client, active_api_key) -> None:
    response = client.get(
        f"/v1/evaluations/jobs?evaluator_name={_OVERSIZED_NAME}",
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 422


def test_list_jobs_filter_128_char_evaluator_name_is_not_a_422(client, active_api_key) -> None:
    response = client.get(
        f"/v1/evaluations/jobs?evaluator_name={_VALID_LENGTH_NAME}",
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 200
    assert response.json() == {"jobs": [], "next_cursor": None}


def test_existing_production_evaluator_names_still_validate_relevance(
    client, active_api_key
) -> None:
    """Explicit regression guard: "relevance" must keep working end-to-end
    through both GET and PUT, exactly as every other test in this file
    already relies on implicitly."""
    put_response = client.put(
        "/v1/evaluations/configs/relevance",
        json={"enabled": True},
        headers=_auth_headers(active_api_key),
    )
    assert put_response.status_code == 200

    get_response = client.get(
        "/v1/evaluations/configs/relevance", headers=_auth_headers(active_api_key)
    )
    assert get_response.status_code == 200
    assert get_response.json()["evaluator_name"] == "relevance"


def test_existing_production_evaluator_names_still_validate_relevance_embedding(
    client, active_api_key
) -> None:
    put_response = client.put(
        "/v1/evaluations/configs/relevance_embedding",
        json={"enabled": True},
        headers=_auth_headers(active_api_key),
    )
    assert put_response.status_code == 200

    get_response = client.get(
        "/v1/evaluations/configs/relevance_embedding", headers=_auth_headers(active_api_key)
    )
    assert get_response.status_code == 200
    assert get_response.json()["evaluator_name"] == "relevance_embedding"


# -- evaluation_jobs status list ----------------------------------------------


def test_list_jobs_requires_customer_auth(client) -> None:
    response = client.get("/v1/evaluations/jobs")
    assert response.status_code == 401


def test_list_jobs_internal_token_does_not_authenticate(client) -> None:
    response = client.get(
        "/v1/evaluations/jobs", headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN}
    )
    assert response.status_code == 401


def test_list_jobs_empty(client, active_api_key) -> None:
    response = client.get("/v1/evaluations/jobs", headers=_auth_headers(active_api_key))
    assert response.status_code == 200
    assert response.json() == {"jobs": [], "next_cursor": None}


def test_list_jobs_returns_created_jobs(client, db_session: Session, active_api_key) -> None:
    job = make_evaluation_job(db_session, active_api_key.project)

    response = client.get("/v1/evaluations/jobs", headers=_auth_headers(active_api_key))
    assert response.status_code == 200
    body = response.json()
    assert len(body["jobs"]) == 1
    returned = body["jobs"][0]
    assert returned["id"] == str(job.id)
    assert returned["status"] == "pending"
    assert returned["trace_id"] == job.trace_id
    assert returned["span_id"] == job.span_id
    assert returned["attempt_count"] == 0
    assert returned["max_retries"] == 3


def test_list_jobs_filters_by_status(client, db_session: Session, active_api_key) -> None:
    make_evaluation_job(db_session, active_api_key.project, status="pending")
    make_evaluation_job(db_session, active_api_key.project, status="dead_letter")

    response = client.get(
        "/v1/evaluations/jobs?status=dead_letter", headers=_auth_headers(active_api_key)
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["status"] == "dead_letter"


def test_list_jobs_filters_by_evaluator_name(client, db_session: Session, active_api_key) -> None:
    make_evaluation_job(db_session, active_api_key.project, evaluator_name="relevance")
    make_evaluation_job(db_session, active_api_key.project, evaluator_name="relevance_embedding")

    response = client.get(
        "/v1/evaluations/jobs?evaluator_name=relevance_embedding",
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["evaluator_name"] == "relevance_embedding"


def test_list_jobs_invalid_status_is_422(client, active_api_key) -> None:
    response = client.get(
        "/v1/evaluations/jobs?status=not-a-real-status", headers=_auth_headers(active_api_key)
    )
    assert response.status_code == 422


def test_list_jobs_malformed_cursor_is_422(client, active_api_key) -> None:
    response = client.get(
        "/v1/evaluations/jobs?cursor=not-a-valid-cursor", headers=_auth_headers(active_api_key)
    )
    assert response.status_code == 422


def test_list_jobs_ordered_most_recent_first(client, db_session: Session, active_api_key) -> None:
    older = make_evaluation_job(db_session, active_api_key.project)
    older.created_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()
    newer = make_evaluation_job(db_session, active_api_key.project)

    response = client.get("/v1/evaluations/jobs", headers=_auth_headers(active_api_key))
    assert response.status_code == 200
    ids = [job["id"] for job in response.json()["jobs"]]
    assert ids == [str(newer.id), str(older.id)]


def test_list_jobs_paginates_with_cursor(client, db_session: Session, active_api_key) -> None:
    for _ in range(3):
        make_evaluation_job(db_session, active_api_key.project)

    first_page = client.get("/v1/evaluations/jobs?limit=2", headers=_auth_headers(active_api_key))
    assert first_page.status_code == 200
    first_body = first_page.json()
    assert len(first_body["jobs"]) == 2
    assert first_body["next_cursor"] is not None

    second_page = client.get(
        f"/v1/evaluations/jobs?limit=2&cursor={first_body['next_cursor']}",
        headers=_auth_headers(active_api_key),
    )
    assert second_page.status_code == 200
    second_body = second_page.json()
    assert len(second_body["jobs"]) == 1
    assert second_body["next_cursor"] is None

    first_page_ids = {job["id"] for job in first_body["jobs"]}
    second_page_ids = {job["id"] for job in second_body["jobs"]}
    assert first_page_ids.isdisjoint(second_page_ids)  # no overlap, no gap (3 jobs total)


def test_list_jobs_scoped_to_project(client, db_session: Session, active_api_key) -> None:
    other_project = _make_project(db_session)
    make_evaluation_job(db_session, other_project)

    response = client.get("/v1/evaluations/jobs", headers=_auth_headers(active_api_key))
    assert response.status_code == 200
    assert response.json() == {"jobs": [], "next_cursor": None}


# -- evaluation_results, span-scoped ------------------------------------------


def test_span_evaluations_requires_customer_auth(client) -> None:
    response = client.get(f"/v1/traces/{TRACE_ID}/spans/{SPAN_ID}/evaluations")
    assert response.status_code == 401


def test_span_evaluations_internal_token_does_not_authenticate(client) -> None:
    response = client.get(
        f"/v1/traces/{TRACE_ID}/spans/{SPAN_ID}/evaluations",
        headers={INTERNAL_TOKEN_HEADER: VALID_INTERNAL_TOKEN},
    )
    assert response.status_code == 401


def test_span_evaluations_empty_when_none_evaluated(client, active_api_key) -> None:
    response = client.get(
        f"/v1/traces/{TRACE_ID}/spans/{SPAN_ID}/evaluations",
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 200
    assert response.json() == {"results": []}


def test_span_evaluations_returns_results(
    client, active_api_key, fake_evaluations_query_repository
) -> None:
    # job_created_at/written_at are naive datetimes here, matching what a
    # real clickhouse_connect client returns for DateTime64 columns (see
    # app.services.evaluations._as_utc's docstring) -- not ISO strings.
    evaluation_id = str(uuid.uuid4())
    fake_evaluations_query_repository.get_span_evaluations_result = [
        {
            "evaluation_id": evaluation_id,
            "trace_id": TRACE_ID,
            "span_id": SPAN_ID,
            "evaluator_name": "relevance",
            "evaluator_version": "0.1.0",
            "score": 0.87,
            "label": "relevant",
            "explanation": "cosine similarity above threshold",
            "evaluator_model": "tfidf",
            "evaluator_provider": None,
            "evaluation_latency_ms": 3.35,
            "evaluation_cost_usd": None,
            "job_created_at": datetime(2026, 9, 11, 12, 0, 0),
            "written_at": datetime(2026, 9, 11, 12, 0, 1),
        }
    ]

    response = client.get(
        f"/v1/traces/{TRACE_ID}/spans/{SPAN_ID}/evaluations",
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["evaluation_id"] == evaluation_id
    assert result["evaluator_name"] == "relevance"
    assert result["score"] == 0.87
    assert result["evaluator_model"] == "tfidf"


def test_span_evaluations_scopes_repository_call_by_project(
    client, active_api_key, fake_evaluations_query_repository
) -> None:
    client.get(
        f"/v1/traces/{TRACE_ID}/spans/{SPAN_ID}/evaluations",
        headers=_auth_headers(active_api_key),
    )
    [call] = fake_evaluations_query_repository.get_span_evaluations_calls
    assert call["project_id"] == active_api_key.project.id
    assert call["trace_id"] == TRACE_ID
    assert call["span_id"] == SPAN_ID


def test_span_evaluations_malformed_trace_id_is_422(client, active_api_key) -> None:
    response = client.get(
        f"/v1/traces/not-valid-hex/spans/{SPAN_ID}/evaluations",
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 422


def test_span_evaluations_malformed_span_id_is_422(client, active_api_key) -> None:
    response = client.get(
        f"/v1/traces/{TRACE_ID}/spans/too-short/evaluations",
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 422


def test_span_evaluations_clickhouse_unavailable_is_503(
    client, active_api_key, fake_evaluations_query_repository
) -> None:
    from app.clickhouse.repository import ClickHouseUnavailableError

    fake_evaluations_query_repository.fail_with = ClickHouseUnavailableError("down")
    response = client.get(
        f"/v1/traces/{TRACE_ID}/spans/{SPAN_ID}/evaluations",
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 503


def test_span_evaluations_clickhouse_query_error_is_500(
    client, active_api_key, fake_evaluations_query_repository
) -> None:
    from app.clickhouse.query_common import ClickHouseQueryError

    fake_evaluations_query_repository.fail_with = ClickHouseQueryError("bad query")
    response = client.get(
        f"/v1/traces/{TRACE_ID}/spans/{SPAN_ID}/evaluations",
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 500
