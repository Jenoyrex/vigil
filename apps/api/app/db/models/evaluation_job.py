import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# Per docs/decisions/005-evaluation-job-storage-worker.md section 1: a
# 5-state model, not 6. `failed` is not a dead end -- a `failed` row with
# attempt_count < max_retries is re-claimable exactly like `pending` (see the
# claim query this table's (status, next_attempt_at) index supports); only
# once attempt_count >= max_retries does the next failure move to the
# terminal `dead_letter` state instead of back to `failed`.
EVALUATION_JOB_STATUSES = ("pending", "running", "succeeded", "failed", "dead_letter")
_STATUSES_SQL_LIST = ", ".join(f"'{status}'" for status in EVALUATION_JOB_STATUSES)


class EvaluationJob(Base, TimestampMixin):
    """One evaluation job: "run evaluator_name/evaluator_version against span
    (project_id, trace_id, span_id) once." -- ADR 005 section 1.

    `trace_id`/`span_id` are OpenTelemetry-format hex strings (32 / 16 hex
    characters), not PostgreSQL UUIDs -- matching
    docs/decisions/002-trace-span-telemetry-model.md section 4 and the
    ClickHouse `spans` table's own `FixedString(32)`/`FixedString(16)`
    columns exactly. This table has no foreign key to ClickHouse (cannot,
    across stores); format validation belongs to the API schema layer (not
    yet built -- Phase 4+), the same layering `apps/api/app/schemas/query.py`
    already uses for `TraceId`/`SpanId`, not a database CHECK here.

    `evaluator_version` (unlike `evaluator_configs`, which deliberately omits
    it -- see evaluator_config.py) IS stored here: only the worker knows the
    currently-installed version for a given evaluator_name, and it is
    supplied once, at job-creation time, becoming part of this row's
    permanent idempotency identity below.
    """

    __tablename__ = "evaluation_jobs"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "trace_id",
            "span_id",
            "evaluator_name",
            "evaluator_version",
            name="uq_evaluation_jobs_idempotency_key",
        ),
        CheckConstraint(f"status IN ({_STATUSES_SQL_LIST})", name="status_valid"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_valid"),
        CheckConstraint("max_retries >= 0", name="max_retries_valid"),
        # Supports the worker's SKIP LOCKED claim query:
        # WHERE status IN ('pending', 'failed') AND next_attempt_at <= now()
        # ORDER BY created_at -- see ADR 005 section 6.
        Index("ix_evaluation_jobs_status_next_attempt_at", "status", "next_attempt_at"),
        # Supports the job-status list API (GET /v1/evaluations/jobs, most
        # recent first, project-scoped) -- not yet built, ADR 005 section 8.
        Index("ix_evaluation_jobs_project_id_created_at", "project_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String, nullable=False)
    span_id: Mapped[str] = mapped_column(String, nullable=False)
    evaluator_name: Mapped[str] = mapped_column(String, nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String, nullable=False)

    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'pending'"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Snapshotted from evaluator_configs.max_retries at job-creation time --
    # deliberately no server_default here beyond a safety-net value, since
    # the intent is every row is populated explicitly at creation (ADR 005
    # section 1).
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))

    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
