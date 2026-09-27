"""Business logic for the session-authenticated workspace routes
(app/api/v1/workspace.py): self-serve organizations, projects and API keys.

Authorization lives here, not only in the routes: every function that
touches a project or organization first resolves the caller's membership
role, and a caller with no membership gets `NotFoundError` -- the same
answer as for an id that doesn't exist, so ids of other tenants leak
nothing. Reuses the existing models and key generation
(app.security.api_keys) exactly as bootstrap provisioning does; bootstrap
itself is untouched and remains the operator-only first-tenant path.
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.api.deps import project_role
from app.db.models import APIKey, Organization, OrganizationMembership, Project, User
from app.schemas.workspace import (
    APIKeyCreateResponse,
    APIKeyListResponse,
    APIKeyOut,
    MeResponse,
    OrganizationOut,
    ProjectOut,
    UserOut,
)
from app.security.api_keys import generate_api_key

MANAGER_ROLES = ("owner", "admin")


class NotFoundError(Exception):
    """The resource doesn't exist, or the caller isn't a member of its org."""


class ForbiddenError(Exception):
    """The caller is a member, but their role can't perform this action."""


def _slug(name: str) -> str:
    # ponytail: random suffix instead of a uniqueness retry loop; 8 hex chars
    # make a collision on the unique slug index practically impossible.
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-") or "workspace"
    return f"{base}-{secrets.token_hex(4)}"


def get_me(db: Session, *, user_id: uuid.UUID) -> MeResponse:
    user = db.get(User, user_id)
    rows = (
        db.query(Organization, OrganizationMembership.role)
        .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
        .filter(OrganizationMembership.user_id == user_id)
        .order_by(Organization.created_at)
        .all()
    )
    org_ids = [org.id for org, _ in rows]
    projects = (
        db.query(Project)
        .filter(Project.organization_id.in_(org_ids))
        .order_by(Project.created_at)
        .all()
        if org_ids
        else []
    )
    return MeResponse(
        user=UserOut(id=user.id, email=user.email, full_name=user.full_name),
        organizations=[
            OrganizationOut(
                id=org.id,
                name=org.name,
                slug=org.slug,
                role=role,
                projects=[_project_out(p) for p in projects if p.organization_id == org.id],
            )
            for org, role in rows
        ],
    )


def _project_out(project: Project) -> ProjectOut:
    return ProjectOut(
        id=project.id, name=project.name, slug=project.slug, created_at=project.created_at
    )


def create_organization(db: Session, *, user_id: uuid.UUID, name: str) -> OrganizationOut:
    """The creator becomes the organization's owner, in the same transaction."""
    organization = Organization(id=uuid.uuid4(), name=name, slug=_slug(name))
    db.add(organization)
    db.flush()
    db.add(OrganizationMembership(user_id=user_id, organization_id=organization.id, role="owner"))
    db.commit()
    return OrganizationOut(
        id=organization.id,
        name=organization.name,
        slug=organization.slug,
        role="owner",
        projects=[],
    )


def create_project(
    db: Session, *, user_id: uuid.UUID, organization_id: uuid.UUID, name: str
) -> ProjectOut:
    role = (
        db.query(OrganizationMembership.role)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
        )
        .scalar()
    )
    if role is None:
        raise NotFoundError
    if role not in MANAGER_ROLES:
        raise ForbiddenError
    project = Project(organization_id=organization_id, name=name, slug=_slug(name))
    db.add(project)
    db.commit()
    return _project_out(project)


def _require_project_role(
    db: Session, *, user_id: uuid.UUID, project_id: uuid.UUID, manage: bool
) -> None:
    role = project_role(db, user_id=user_id, project_id=project_id)
    if role is None:
        raise NotFoundError
    if manage and role not in MANAGER_ROLES:
        raise ForbiddenError


def _key_out(key: APIKey) -> APIKeyOut:
    return APIKeyOut(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        status=key.status,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
    )


def list_api_keys(db: Session, *, user_id: uuid.UUID, project_id: uuid.UUID) -> APIKeyListResponse:
    _require_project_role(db, user_id=user_id, project_id=project_id, manage=False)
    keys = (
        db.query(APIKey)
        .filter(APIKey.project_id == project_id)
        .order_by(APIKey.created_at.desc())
        .all()
    )
    return APIKeyListResponse(items=[_key_out(k) for k in keys])


def create_api_key(
    db: Session, *, user_id: uuid.UUID, project_id: uuid.UUID, name: str
) -> APIKeyCreateResponse:
    _require_project_role(db, user_id=user_id, project_id=project_id, manage=True)
    raw_key, key_prefix, key_hash = generate_api_key()
    key = APIKey(
        project_id=project_id,
        name=name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        created_by=user_id,
    )
    db.add(key)
    db.commit()
    return APIKeyCreateResponse(**_key_out(key).model_dump(), api_key=raw_key)


def revoke_api_key(
    db: Session, *, user_id: uuid.UUID, project_id: uuid.UUID, key_id: uuid.UUID
) -> APIKeyOut:
    """Idempotent: revoking an already-revoked key returns it unchanged."""
    _require_project_role(db, user_id=user_id, project_id=project_id, manage=True)
    key = db.query(APIKey).filter(APIKey.id == key_id, APIKey.project_id == project_id).first()
    if key is None:
        raise NotFoundError
    if key.status != "revoked":
        key.status = "revoked"
        key.revoked_at = datetime.now(UTC)
        db.commit()
    return _key_out(key)
