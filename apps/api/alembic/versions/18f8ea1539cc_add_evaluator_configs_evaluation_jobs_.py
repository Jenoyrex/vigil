"""add evaluator configs, evaluation jobs, poller checkpoint

Per docs/decisions/005-evaluation-job-storage-worker.md sections 1, 2, 4, and
7: the three PostgreSQL tables ADR 005 identifies as the minimum required
schema for production evaluation job/storage/worker integration.
`evaluator_configs`/`evaluation_jobs` mirror
apps/api/app/db/models/evaluator_config.py and evaluation_job.py exactly;
`evaluation_poller_checkpoint` mirrors evaluation_poller_checkpoint.py. No
ClickHouse, worker, or API route changes accompany this migration -- those
are separate, later phases per ADR 005 section 15.

Revision ID: 18f8ea1539cc
Revises: e5af6d55da1b
Create Date: 2026-09-03 20:32:07.189901

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '18f8ea1539cc'
down_revision: Union[str, Sequence[str], None] = 'e5af6d55da1b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'evaluator_configs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('evaluator_name', sa.String(), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('sampling_rate', sa.Float(), server_default=sa.text('0.1'), nullable=False),
        sa.Column('threshold', sa.Float(), nullable=True),
        sa.Column('max_retries', sa.Integer(), server_default=sa.text('3'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('sampling_rate >= 0 AND sampling_rate <= 1', name=op.f('ck_evaluator_configs_sampling_rate_valid')),
        sa.CheckConstraint('max_retries >= 0', name=op.f('ck_evaluator_configs_max_retries_valid')),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_evaluator_configs_project_id_projects'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_evaluator_configs')),
        sa.UniqueConstraint('project_id', 'evaluator_name', name='uq_evaluator_configs_project_id_evaluator_name'),
    )

    op.create_table(
        'evaluation_jobs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('trace_id', sa.String(), nullable=False),
        sa.Column('span_id', sa.String(), nullable=False),
        sa.Column('evaluator_name', sa.String(), nullable=False),
        sa.Column('evaluator_version', sa.String(), nullable=False),
        sa.Column('status', sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column('attempt_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('max_retries', sa.Integer(), server_default=sa.text('3'), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('claimed_by', sa.String(), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'running', 'succeeded', 'failed', 'dead_letter')", name=op.f('ck_evaluation_jobs_status_valid')),
        sa.CheckConstraint('attempt_count >= 0', name=op.f('ck_evaluation_jobs_attempt_count_valid')),
        sa.CheckConstraint('max_retries >= 0', name=op.f('ck_evaluation_jobs_max_retries_valid')),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_evaluation_jobs_project_id_projects'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_evaluation_jobs')),
        sa.UniqueConstraint('project_id', 'trace_id', 'span_id', 'evaluator_name', 'evaluator_version', name='uq_evaluation_jobs_idempotency_key'),
    )
    op.create_index('ix_evaluation_jobs_status_next_attempt_at', 'evaluation_jobs', ['status', 'next_attempt_at'], unique=False)
    op.create_index('ix_evaluation_jobs_project_id_created_at', 'evaluation_jobs', ['project_id', 'created_at'], unique=False)

    op.create_table(
        'evaluation_poller_checkpoint',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('last_ingested_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_evaluation_poller_checkpoint')),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('evaluation_poller_checkpoint')

    op.drop_index('ix_evaluation_jobs_project_id_created_at', table_name='evaluation_jobs')
    op.drop_index('ix_evaluation_jobs_status_next_attempt_at', table_name='evaluation_jobs')
    op.drop_table('evaluation_jobs')

    op.drop_table('evaluator_configs')
