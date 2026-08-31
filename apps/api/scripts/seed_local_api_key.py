"""Local-development helper: mint an API key for a demo project.

There is no key-issuance HTTP endpoint yet (out of scope for the ingestion
API) -- this script is the supported way to get a usable key for manual
testing against a local Postgres + ClickHouse setup. It is idempotent for
the org/project (reuses them by slug if they already exist) but always
issues a brand-new API key, since the raw key cannot be recovered once
generated.

Usage (from apps/api):

    uv run python scripts/seed_local_api_key.py
"""

from __future__ import annotations

from app.db.models import APIKey, Organization, Project
from app.db.session import SessionLocal
from app.security.api_keys import generate_api_key

DEMO_ORG_SLUG = "local-dev"
DEMO_PROJECT_SLUG = "local-dev"


def main() -> None:
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == DEMO_ORG_SLUG).first()
        if org is None:
            org = Organization(name="Local Dev", slug=DEMO_ORG_SLUG)
            db.add(org)
            db.flush()

        project = (
            db.query(Project)
            .filter(Project.organization_id == org.id, Project.slug == DEMO_PROJECT_SLUG)
            .first()
        )
        if project is None:
            project = Project(organization_id=org.id, name="Local Dev", slug=DEMO_PROJECT_SLUG)
            db.add(project)
            db.flush()

        raw_key, key_prefix, key_hash = generate_api_key()
        api_key = APIKey(
            project_id=project.id,
            name="Local dev key",
            key_prefix=key_prefix,
            key_hash=key_hash,
        )
        db.add(api_key)
        db.commit()

        print(f"project_id: {project.id}")
        print(f"api_key_id: {api_key.id}")
        print(f"api_key:    {raw_key}")
        print()
        print("This key is shown once and cannot be recovered -- copy it now.")
        print(
            "Example: curl -X POST http://127.0.0.1:8000/v1/traces \\\n"
            f'  -H "Authorization: Bearer {raw_key}" \\\n'
            '  -H "Content-Type: application/json" -d @payload.json'
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
