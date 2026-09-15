"""add provisioning bootstrap

Phase 4D, F3 (production provisioning/onboarding): a single-row table
recording whether the one-time `POST /v1/provisioning/bootstrap` endpoint
has run, mirroring apps/api/app/db/models/evaluation_poller_checkpoint.py's
existing fixed-string-primary-key singleton-row idiom. See
app/db/models/provisioning_bootstrap.py and app/services/provisioning.py
for why this row -- inserted with `ON CONFLICT (id) DO NOTHING` -- is the
database-enforced concurrency gate that makes bootstrap safe under
concurrent requests, not merely an application-level check.

Revision ID: 2925f09e0276
Revises: 18f8ea1539cc
Create Date: 2026-09-15 09:20:08.776078

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2925f09e0276'
down_revision: Union[str, Sequence[str], None] = '18f8ea1539cc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'provisioning_bootstrap',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_provisioning_bootstrap_organization_id_organizations'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_provisioning_bootstrap')),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('provisioning_bootstrap')
