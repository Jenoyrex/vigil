import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    APIKey,
    EvaluationJob,
    EvaluationPollerCheckpoint,
    EvaluatorConfig,
    Organization,
    OrganizationMembership,
    Project,
    User,
)
from app.db.models.evaluation_poller_checkpoint import SINGLETON_ROW_ID


def make_user(db: Session, **overrides) -> User:
    defaults = {"email": f"{uuid.uuid4()}@example.com", "full_name": "Test User"}
    user = User(**{**defaults, **overrides})
    db.add(user)
    db.commit()
    return user


def make_organization(db: Session, **overrides) -> Organization:
    unique = uuid.uuid4().hex[:8]
    defaults = {"name": f"Org {unique}", "slug": f"org-{unique}"}
    org = Organization(**{**defaults, **overrides})
    db.add(org)
    db.commit()
    return org


def make_project(db: Session, organization: Organization, **overrides) -> Project:
    unique = uuid.uuid4().hex[:8]
    defaults = {
        "organization_id": organization.id,
        "name": f"Project {unique}",
        "slug": f"project-{unique}",
    }
    project = Project(**{**defaults, **overrides})
    db.add(project)
    db.commit()
    return project


def make_api_key(db: Session, project: Project, **overrides) -> APIKey:
    unique = uuid.uuid4().hex[:8]
    defaults = {
        "project_id": project.id,
        "name": "Test key",
        "key_prefix": f"vgl_{unique[:6]}",
        "key_hash": f"hash-{unique}",
    }
    key = APIKey(**{**defaults, **overrides})
    db.add(key)
    db.commit()
    return key


def make_evaluator_config(db: Session, project: Project, **overrides) -> EvaluatorConfig:
    defaults = {"project_id": project.id, "evaluator_name": "relevance"}
    config = EvaluatorConfig(**{**defaults, **overrides})
    db.add(config)
    db.commit()
    return config


def make_evaluation_job(db: Session, project: Project, **overrides) -> EvaluationJob:
    unique = uuid.uuid4().hex
    defaults = {
        "project_id": project.id,
        "trace_id": unique[:32].ljust(32, "0"),
        "span_id": unique[:16].ljust(16, "0"),
        "evaluator_name": "relevance",
        "evaluator_version": "0.1.0",
    }
    job = EvaluationJob(**{**defaults, **overrides})
    db.add(job)
    db.commit()
    return job


def test_user_can_be_created(db_session: Session) -> None:
    user = make_user(db_session)

    fetched = db_session.get(User, user.id)
    assert fetched is not None
    assert fetched.email == user.email


def test_organization_can_be_created(db_session: Session) -> None:
    org = make_organization(db_session)

    fetched = db_session.get(Organization, org.id)
    assert fetched is not None
    assert fetched.slug == org.slug


def test_membership_connects_user_and_organization(db_session: Session) -> None:
    user = make_user(db_session)
    org = make_organization(db_session)

    membership = OrganizationMembership(user_id=user.id, organization_id=org.id, role="member")
    db_session.add(membership)
    db_session.commit()

    fetched = db_session.get(OrganizationMembership, membership.id)
    assert fetched.user_id == user.id
    assert fetched.organization_id == org.id


def test_duplicate_organization_membership_is_rejected(db_session: Session) -> None:
    user = make_user(db_session)
    org = make_organization(db_session)
    db_session.add(OrganizationMembership(user_id=user.id, organization_id=org.id, role="member"))
    db_session.commit()

    db_session.add(OrganizationMembership(user_id=user.id, organization_id=org.id, role="admin"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_project_belongs_to_organization(db_session: Session) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)

    fetched = db_session.get(Project, project.id)
    assert fetched.organization_id == org.id


def test_duplicate_project_slug_within_same_organization_is_rejected(db_session: Session) -> None:
    org = make_organization(db_session)
    make_project(db_session, org, slug="checkout")

    db_session.add(Project(organization_id=org.id, name="Other", slug="checkout"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_same_project_slug_in_different_organizations_is_allowed(db_session: Session) -> None:
    org_a = make_organization(db_session)
    org_b = make_organization(db_session)
    make_project(db_session, org_a, slug="checkout")

    project_b = Project(organization_id=org_b.id, name="Checkout", slug="checkout")
    db_session.add(project_b)
    db_session.commit()

    assert db_session.get(Project, project_b.id) is not None


def test_api_key_belongs_to_project(db_session: Session) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)
    key = make_api_key(db_session, project)

    fetched = db_session.get(APIKey, key.id)
    assert fetched.project_id == project.id


def test_duplicate_api_key_hash_is_rejected(db_session: Session) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)
    make_api_key(db_session, project, key_hash="duplicate-hash")

    db_session.add(
        APIKey(
            project_id=project.id,
            name="Another key",
            key_prefix="vgl_dup",
            key_hash="duplicate-hash",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_api_key_status_accepts_active_and_revoked(db_session: Session) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)

    active_key = make_api_key(db_session, project, status="active")
    revoked_key = make_api_key(db_session, project, status="revoked")

    assert db_session.get(APIKey, active_key.id).status == "active"
    assert db_session.get(APIKey, revoked_key.id).status == "revoked"


def test_api_key_status_rejects_invalid_values(db_session: Session) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)

    db_session.add(
        APIKey(
            project_id=project.id,
            name="Bad status key",
            key_prefix="vgl_bad",
            key_hash="bad-status-hash",
            status="disabled",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_deleting_user_sets_api_key_created_by_to_null(db_session: Session) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)
    creator = make_user(db_session)
    key = make_api_key(db_session, project, created_by=creator.id)

    db_session.delete(creator)
    db_session.commit()

    db_session.expire_all()
    fetched = db_session.get(APIKey, key.id)
    assert fetched.created_by is None


def test_deleting_organization_cascades_memberships(db_session: Session) -> None:
    user = make_user(db_session)
    org = make_organization(db_session)
    membership = OrganizationMembership(user_id=user.id, organization_id=org.id, role="owner")
    db_session.add(membership)
    db_session.commit()
    membership_id = membership.id

    db_session.delete(org)
    db_session.commit()

    db_session.expire_all()
    assert db_session.get(OrganizationMembership, membership_id) is None
    assert db_session.get(User, user.id) is not None


def test_deleting_organization_is_restricted_when_projects_exist(db_session: Session) -> None:
    org = make_organization(db_session)
    make_project(db_session, org)

    db_session.delete(org)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# -- EvaluatorConfig (docs/decisions/005-evaluation-job-storage-worker.md section 2) -------------


def test_evaluator_config_belongs_to_project(db_session: Session) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)
    config = make_evaluator_config(db_session, project)

    fetched = db_session.get(EvaluatorConfig, config.id)
    assert fetched.project_id == project.id
    assert fetched.evaluator_name == "relevance"


def test_evaluator_config_defaults_are_conservative(db_session: Session) -> None:
    """Per ADR 005 section 12: enabled defaults false (opt-in), sampling_rate
    defaults to 0.1 (not 1.0 -- conservative, not full-volume), threshold
    defaults to NULL (fall back to the evaluator's own code-level
    DEFAULT_THRESHOLD, never a WikiQA-selected number), max_retries
    defaults to 3."""
    org = make_organization(db_session)
    project = make_project(db_session, org)
    config = make_evaluator_config(db_session, project)

    assert config.enabled is False
    assert config.sampling_rate == pytest.approx(0.1)
    assert config.threshold is None
    assert config.max_retries == 3


def test_duplicate_evaluator_config_for_same_project_and_name_is_rejected(
    db_session: Session,
) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)
    make_evaluator_config(db_session, project, evaluator_name="relevance")

    db_session.add(EvaluatorConfig(project_id=project.id, evaluator_name="relevance"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_same_evaluator_name_in_different_projects_is_allowed(db_session: Session) -> None:
    org = make_organization(db_session)
    project_a = make_project(db_session, org)
    project_b = make_project(db_session, org)
    make_evaluator_config(db_session, project_a, evaluator_name="relevance")

    config_b = EvaluatorConfig(project_id=project_b.id, evaluator_name="relevance")
    db_session.add(config_b)
    db_session.commit()

    assert db_session.get(EvaluatorConfig, config_b.id) is not None


def test_different_evaluator_names_in_same_project_are_allowed(db_session: Session) -> None:
    """`relevance` (TF-IDF) and `relevance_embedding` (BGE) must be
    independently selectable per project -- ADR 004."""
    org = make_organization(db_session)
    project = make_project(db_session, org)
    make_evaluator_config(db_session, project, evaluator_name="relevance")

    config_2 = EvaluatorConfig(project_id=project.id, evaluator_name="relevance_embedding")
    db_session.add(config_2)
    db_session.commit()

    assert db_session.get(EvaluatorConfig, config_2.id) is not None


@pytest.mark.parametrize("bad_rate", [-0.1, 1.1])
def test_evaluator_config_rejects_out_of_range_sampling_rate(
    db_session: Session, bad_rate: float
) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)

    db_session.add(
        EvaluatorConfig(project_id=project.id, evaluator_name="relevance", sampling_rate=bad_rate)
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.parametrize("boundary_rate", [0.0, 1.0])
def test_evaluator_config_accepts_boundary_sampling_rates(
    db_session: Session, boundary_rate: float
) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)

    config = make_evaluator_config(db_session, project, sampling_rate=boundary_rate)
    assert db_session.get(EvaluatorConfig, config.id).sampling_rate == pytest.approx(boundary_rate)


def test_evaluator_config_rejects_negative_max_retries(db_session: Session) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)

    db_session.add(
        EvaluatorConfig(project_id=project.id, evaluator_name="relevance", max_retries=-1)
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_evaluator_config_threshold_can_be_overridden_per_project(db_session: Session) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)
    config = make_evaluator_config(db_session, project, threshold=0.42)

    assert db_session.get(EvaluatorConfig, config.id).threshold == pytest.approx(0.42)


def test_deleting_project_is_restricted_when_evaluator_config_exists(db_session: Session) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)
    make_evaluator_config(db_session, project)

    db_session.delete(project)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# -- EvaluationJob (docs/decisions/005-evaluation-job-storage-worker.md section 1) ---------------


def test_evaluation_job_belongs_to_project(db_session: Session) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)
    job = make_evaluation_job(db_session, project)

    fetched = db_session.get(EvaluationJob, job.id)
    assert fetched.project_id == project.id


def test_evaluation_job_defaults_to_pending_with_zero_attempts(db_session: Session) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)
    job = make_evaluation_job(db_session, project)

    assert job.status == "pending"
    assert job.attempt_count == 0
    assert job.max_retries == 3
    assert job.next_attempt_at is None
    assert job.claimed_at is None
    assert job.claimed_by is None
    assert job.last_error is None


def test_duplicate_evaluation_job_idempotency_key_is_rejected(db_session: Session) -> None:
    """(project_id, trace_id, span_id, evaluator_name, evaluator_version) is
    the job's logical identity -- ADR 004 section 5, ADR 005 section 1.
    Re-enqueuing the same evaluator version against the same span must be
    rejected at the database level."""
    org = make_organization(db_session)
    project = make_project(db_session, org)
    job = make_evaluation_job(db_session, project)

    db_session.add(
        EvaluationJob(
            project_id=project.id,
            trace_id=job.trace_id,
            span_id=job.span_id,
            evaluator_name=job.evaluator_name,
            evaluator_version=job.evaluator_version,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_evaluation_job_with_different_evaluator_version_is_a_distinct_job(
    db_session: Session,
) -> None:
    """A new evaluator_version intentionally produces a new, distinct job
    for an already-evaluated span -- ADR 005 section 3, not a duplicate."""
    org = make_organization(db_session)
    project = make_project(db_session, org)
    job = make_evaluation_job(db_session, project, evaluator_version="0.1.0")

    job_v2 = EvaluationJob(
        project_id=project.id,
        trace_id=job.trace_id,
        span_id=job.span_id,
        evaluator_name=job.evaluator_name,
        evaluator_version="0.2.0",
    )
    db_session.add(job_v2)
    db_session.commit()

    assert db_session.get(EvaluationJob, job_v2.id) is not None


def test_evaluation_job_with_different_evaluator_name_is_a_distinct_job(
    db_session: Session,
) -> None:
    """The same span may independently be evaluated by both `relevance` and
    `relevance_embedding` -- ADR 004's independent-selectability
    requirement."""
    org = make_organization(db_session)
    project = make_project(db_session, org)
    job = make_evaluation_job(db_session, project, evaluator_name="relevance")

    job_embedding = EvaluationJob(
        project_id=project.id,
        trace_id=job.trace_id,
        span_id=job.span_id,
        evaluator_name="relevance_embedding",
        evaluator_version="0.1.0",
    )
    db_session.add(job_embedding)
    db_session.commit()

    assert db_session.get(EvaluationJob, job_embedding.id) is not None


@pytest.mark.parametrize("status", ["pending", "running", "succeeded", "failed", "dead_letter"])
def test_evaluation_job_status_accepts_every_lifecycle_state(
    db_session: Session, status: str
) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)
    job = make_evaluation_job(db_session, project, status=status)

    assert db_session.get(EvaluationJob, job.id).status == status


def test_evaluation_job_status_rejects_invalid_values(db_session: Session) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)

    db_session.add(
        EvaluationJob(
            project_id=project.id,
            trace_id="a" * 32,
            span_id="b" * 16,
            evaluator_name="relevance",
            evaluator_version="0.1.0",
            status="in_progress",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_evaluation_job_rejects_negative_attempt_count(db_session: Session) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)

    db_session.add(
        EvaluationJob(
            project_id=project.id,
            trace_id="a" * 32,
            span_id="b" * 16,
            evaluator_name="relevance",
            evaluator_version="0.1.0",
            attempt_count=-1,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_deleting_project_is_restricted_when_evaluation_job_exists(db_session: Session) -> None:
    org = make_organization(db_session)
    project = make_project(db_session, org)
    make_evaluation_job(db_session, project)

    db_session.delete(project)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# -- EvaluationPollerCheckpoint (docs/decisions/005-evaluation-job-storage-worker.md section 7) --


def test_evaluation_poller_checkpoint_can_be_created_with_singleton_id(
    db_session: Session,
) -> None:
    checkpoint = EvaluationPollerCheckpoint(id=SINGLETON_ROW_ID)
    db_session.add(checkpoint)
    db_session.commit()

    fetched = db_session.get(EvaluationPollerCheckpoint, SINGLETON_ROW_ID)
    assert fetched is not None
    assert fetched.last_ingested_at is None


def test_duplicate_singleton_checkpoint_row_is_rejected(db_session: Session) -> None:
    db_session.add(EvaluationPollerCheckpoint(id=SINGLETON_ROW_ID))
    db_session.commit()

    db_session.add(EvaluationPollerCheckpoint(id=SINGLETON_ROW_ID))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
