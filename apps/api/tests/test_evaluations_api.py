"""API tests for POST /v1/evaluations/jobs -- ADR 005 sections 9/10, Phase
3H amendment.

Uses the `client` fixture (real Postgres test DB, fake
TracesQueryRepository) exactly like test_traces_api.py-style tests already
do for the customer-facing endpoints -- `get_traces_query_repository` is
imported and reused directly from app.api.v1.traces (not redefined) so the
SAME fixture override already wired in tests/conftest.py's `client` fixture
covers this new route too.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.db.models import EvaluationJob
from test_models import make_evaluator_config, make_organization, make_project

INTERNAL_TOKEN_HEADER = "X-Vigil-Internal-Token"
VALID_INTERNAL_TOKEN = "test-internal-service-token"  # matches conftest.py's os.environ.setdefault

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"


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
