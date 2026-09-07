import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# Default sampling_rate (0.1, not 1.0) is deliberately conservative -- per
# docs/decisions/005-evaluation-job-storage-worker.md section 12, an evaluator
# that has just been opted into (enabled=true) should not silently evaluate
# every eligible span at full volume; the embedding evaluator in particular
# runs ~100x slower per call than the TF-IDF baseline
# (services/evaluator/validation/reports/wikiqa_comparison.md), so a low
# default bounds worst-case worker load/cost until an operator makes a
# deliberate choice to raise it.
DEFAULT_SAMPLING_RATE = 0.1

# Default max_retries for a project's evaluator config; snapshotted onto each
# evaluation_jobs row at job-creation time (see evaluation_job.py) so a later
# config edit never retroactively changes an in-flight job's retry policy.
DEFAULT_MAX_RETRIES = 3


class EvaluatorConfig(Base, TimestampMixin):
    """Per-project, per-evaluator configuration -- ADR 005 section 2.

    `evaluator_name` is a free string, not a database enum, matching
    `span_type`'s precedent (docs/decisions/002-trace-span-telemetry-model.md
    section 5): a new evaluator shipped in services/evaluator must never
    require a migration here to become configurable.

    `evaluator_version` is deliberately NOT a column on this table -- see ADR
    005 section 2. Only the worker (which imports services/evaluator) knows
    the currently-installed version for a given evaluator_name; apps/api must
    not gain that dependency just to store a version string.

    `threshold` is nullable by design: NULL means "use this evaluator's own
    code-level DEFAULT_THRESHOLD" (services/evaluator/app/relevance.py and
    app/embedding_relevance.py each already document that constant as an
    unvalidated operational placeholder, not a product decision). This table
    never stores a WikiQA-selected number (0.17 / 0.89) as a default -- ADR
    005 section 12 is explicit that neither is validated for production
    traffic.
    """

    __tablename__ = "evaluator_configs"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "evaluator_name", name="uq_evaluator_configs_project_id_evaluator_name"
        ),
        CheckConstraint(
            "sampling_rate >= 0 AND sampling_rate <= 1", name="sampling_rate_valid"
        ),
        CheckConstraint("max_retries >= 0", name="max_retries_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    evaluator_name: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    sampling_rate: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text(str(DEFAULT_SAMPLING_RATE))
    )
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_retries: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(DEFAULT_MAX_RETRIES))
    )
