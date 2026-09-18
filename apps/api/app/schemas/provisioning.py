"""Request/response schemas for `POST /v1/provisioning/bootstrap` (Phase
4D, F3; extended in Phase 4D F1 to also create the first dashboard user)
-- see app/api/v1/provisioning.py's module docstring for the endpoint's
full design and security model. Bootstrap-token-authenticated only, never
customer-facing.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

# Deliberately re-declared, not imported from app.schemas.auth -- see that
# module's own comment on _EMAIL_RE for the "duplicate a small validator
# rather than couple unrelated schema modules together" precedent
# (app/schemas/query.py's module docstring) this mirrors.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _validate_owner_email(value: str) -> str:
    normalized = value.strip().lower()
    if not _EMAIL_RE.fullmatch(normalized):
        raise ValueError("must be a valid email address.")
    return normalized


OwnerEmail = Annotated[str, AfterValidator(_validate_owner_email)]

# Lowercase, URL-safe identifier: letters, digits, hyphens, matching
# app/db/models/organization.py's/project.py's own `slug` columns (unique
# per-table/per-organization respectively, but format-unconstrained at the
# database level -- this is where format validation belongs, mirroring
# app/schemas/evaluations.py's identical EVALUATOR_NAME_RE precedent for
# why: bounding it here, in the schema layer, turns a malformed value into
# a clean 422 instead of an obscure database error surfacing from deep
# inside app/services/provisioning.py.
_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")


def _validate_slug(value: str) -> str:
    if not _SLUG_RE.fullmatch(value):
        raise ValueError(
            "must be 1-64 characters, lowercase letters/digits/hyphens only, "
            "and cannot start or end with a hyphen."
        )
    return value


Slug = Annotated[str, AfterValidator(_validate_slug)]


class BootstrapRequest(BaseModel):
    """Everything needed to create the one organization/project/API
    key/dashboard-owner-user this endpoint ever creates. Every field is
    operator-supplied naming or credentials -- nothing here lets a caller
    target an existing organization/project/user or influence which one
    wins the bootstrap race (see the service module).

    `owner_password` is never persisted in plaintext: `app.services.
    provisioning.bootstrap_provisioning` hashes it with
    `app.security.passwords.hash_password` (scrypt) before it ever reaches
    a database row, and it is never included in any log line or in
    `BootstrapResponse`.
    """

    organization_name: str = Field(min_length=1, max_length=200)
    organization_slug: Slug
    project_name: str = Field(min_length=1, max_length=200)
    project_slug: Slug
    api_key_name: str = Field(default="Bootstrap key", min_length=1, max_length=200)
    owner_email: OwnerEmail
    owner_full_name: str | None = Field(default=None, max_length=200)
    # A real minimum for an account with full dashboard access, not merely
    # "non-empty" -- this is the one password this endpoint ever sets, and
    # unlike a login attempt (app.schemas.auth.LoginRequest.password, no
    # strength constraint) this is account *creation*, the one point where
    # enforcing a floor actually prevents a weak credential from ever
    # existing in the first place.
    owner_password: str = Field(min_length=12, max_length=200)


class BootstrapResponse(BaseModel):
    """`api_key` is the plaintext key -- shown exactly once, here, and never
    again: it is not persisted anywhere (app/security/api_keys.py only ever
    stores its SHA-256 hash), and there is no endpoint that can retrieve it
    later. Copy it now, per `warning`. `owner_password` is deliberately
    never echoed back at all -- the operator already knows it, having just
    chosen it, so unlike the API key there is nothing to "show once" here.
    """

    organization_id: uuid.UUID
    project_id: uuid.UUID
    api_key_id: uuid.UUID
    api_key: str
    user_id: uuid.UUID
    owner_email: str
    warning: str = (
        "This API key is shown only once and cannot be retrieved again. "
        "Store it securely now. There is no HTTP endpoint to rotate or "
        "revoke it -- see apps/api/README.md's Provisioning section for "
        "the direct-database procedure."
    )
