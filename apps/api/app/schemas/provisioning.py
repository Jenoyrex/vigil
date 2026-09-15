"""Request/response schemas for `POST /v1/provisioning/bootstrap` (Phase
4D, F3) -- see app/api/v1/provisioning.py's module docstring for the
endpoint's full design and security model. Bootstrap-token-authenticated
only, never customer-facing.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

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
    """Everything needed to create the one organization/project/API key
    this endpoint ever creates. Every field is operator-supplied naming --
    nothing here lets a caller target an existing organization/project or
    influence which one wins the bootstrap race (see the service module).
    """

    organization_name: str = Field(min_length=1, max_length=200)
    organization_slug: Slug
    project_name: str = Field(min_length=1, max_length=200)
    project_slug: Slug
    api_key_name: str = Field(default="Bootstrap key", min_length=1, max_length=200)


class BootstrapResponse(BaseModel):
    """`api_key` is the plaintext key -- shown exactly once, here, and never
    again: it is not persisted anywhere (app/security/api_keys.py only ever
    stores its SHA-256 hash), and there is no endpoint that can retrieve it
    later. Copy it now, per `warning`.
    """

    organization_id: uuid.UUID
    project_id: uuid.UUID
    api_key_id: uuid.UUID
    api_key: str
    warning: str = (
        "This API key is shown only once and cannot be retrieved again. "
        "Store it securely now. There is no HTTP endpoint to rotate or "
        "revoke it -- see apps/api/README.md's Provisioning section for "
        "the direct-database procedure."
    )
