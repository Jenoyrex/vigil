import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import APIKey, Organization, OrganizationMembership, Project, User


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
