# 3. ClickHouse Telemetry Storage

- Status: Accepted
- Date: 2026-08-31

## Context

ADR 002 defined the trace/span data model, the ingestion envelope, and a proposed ClickHouse
partitioning/ordering strategy, but deferred the exact ClickHouse column types, the full table
definition, concrete retention/payload limits, and the precise semantics of deduplication. Those
decisions need to be settled before the `spans` table is created, because the column types,
engine, and TTL expression are expensive to change once telemetry volume is flowing, and because
the payload-truncation representation determines what the ingestion API must compute before every
insert.

This ADR does not create ClickHouse configuration, tables, or migrations, install dependencies,
configure Docker, implement the ingestion API, or implement Redis. It documents the approved
storage design only; implementation is separate, later work. It amends ADR 002's proposed
partitioning (monthly → daily); all other ADR 002 decisions are carried forward unchanged.

## Decision

### 1. ClickHouse as telemetry storage

ClickHouse holds the `spans` table and the derived `traces` materialized view: high-volume,
append-only, time-ranged/aggregated telemetry. This is unchanged from ADR 001/002 — this ADR
finalizes the schema those decisions anticipated, it does not revisit the choice of ClickHouse
itself.

### 2–4. Final `spans` table schema, types, engine, keys

```sql
CREATE TABLE spans
(
    project_id            UUID,
    trace_id              FixedString(32),
    span_id                FixedString(16),
    parent_span_id        Nullable(FixedString(16)),

    name                  String,
    span_type             LowCardinality(String),
    resource              LowCardinality(String),

    start_time            DateTime64(3),
    end_time              DateTime64(3),
    duration_ms           UInt32 MATERIALIZED dateDiff('millisecond', start_time, end_time),

    status                Enum8('unset' = 0, 'ok' = 1, 'error' = 2) DEFAULT 'unset',
    status_message        Nullable(String),

    input                 Nullable(String),
    input_size_bytes      UInt32 DEFAULT 0,
    input_truncated       Bool DEFAULT false,

    output                Nullable(String),
    output_size_bytes     UInt32 DEFAULT 0,
    output_truncated      Bool DEFAULT false,

    attributes            Map(LowCardinality(String), String),
    attributes_truncated  Bool DEFAULT false,

    events Nested
    (
        time              DateTime64(3),
        name              LowCardinality(String),
        attributes        Map(LowCardinality(String), String)
    ),
    events_truncated      Bool DEFAULT false,

    llm_provider          LowCardinality(Nullable(String)),
    llm_model             LowCardinality(Nullable(String)),
    llm_input_tokens      Nullable(UInt32),
    llm_output_tokens     Nullable(UInt32),
    llm_total_tokens      Nullable(UInt32),
    llm_cost_usd          Nullable(Decimal64(6)),

    environment           LowCardinality(String),
    release               LowCardinality(Nullable(String)),

    ingested_at           DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toDate(start_time)
ORDER BY (project_id, toDate(start_time), trace_id, span_id)
TTL toDate(start_time) + INTERVAL 30 DAY DELETE;
```

Type rationale for the choices that depart from a naive `String`/`Nullable` default:

- `trace_id` / `span_id` / `parent_span_id` use `FixedString(32)` / `FixedString(16)` rather than
  `String`, since OpenTelemetry-format IDs are always exactly 32 / 16 hex characters — a fixed
  width is cheaper to store and compare than a variable-length string.
- `parent_span_id` is `Nullable`, not a sentinel value, because ADR 002 defines the root span as
  the span where `parent_span_id IS NULL`; a sentinel would make that check implicit and fragile.
- `status` is a closed `Enum8`, unlike `span_type`'s open `LowCardinality(String)`, because ADR 002
  fixes status to exactly three OpenTelemetry-defined values, whereas `span_type` is explicitly
  open-ended and must never require a migration to accept a new value.
- `duration_ms` is `MATERIALIZED`, not a client-supplied column, so it is always internally
  consistent with `start_time`/`end_time` and is never sent redundantly over the wire.
- `attributes` is `Map(LowCardinality(String), String)` rather than a raw JSON string, so
  individual namespaced keys (`retrieval.query`, `tool.name`) can be queried and filtered directly
  without a JSON-parsing function call, while still accepting arbitrary keys.
- `events` uses ClickHouse's `Nested` type to represent a variable-length list of timestamped
  sub-records (OpenTelemetry span events), expanding internally to parallel arrays
  (`events.time`, `events.name`, `events.attributes`).
- `llm_cost_usd` is `Decimal64(6)`, not `Float64`, so that summing cost across millions of rows
  does not accumulate floating-point rounding error.
- LLM token/cost columns and `llm_provider`/`llm_model` are `Nullable` rather than defaulted to 0
  or `''`, so "not an LLM span" is distinguishable from "an LLM span that used zero tokens."

### 5. `ENGINE = ReplacingMergeTree(ingested_at)`

See "Deduplication semantics" below.

### 6. `ORDER BY (project_id, toDate(start_time), trace_id, span_id)`

Unchanged from ADR 002: tenant first for isolation and pruning, date next for time-range pruning,
`trace_id` next so a whole trace's spans are physically colocated for fast single-trace reads,
`span_id` last for uniqueness within that ordering.

### 7. `PARTITION BY toDate(start_time)` and 30-day TTL (revises ADR 002)

ADR 002 originally proposed `PARTITION BY toYYYYMM(start_time)` (monthly partitions). This ADR
revises that to **daily partitions**: `PARTITION BY toDate(start_time)`.

With a 30-day retention window, monthly partitions do not expire atomically: a given month's
partition contains rows spanning up to 31 different days, each crossing the 30-day TTL threshold
on a different calendar day. ClickHouse can only drop a partition wholesale once every row in it
has expired, so under monthly partitioning the partition containing day 1 of a month can't be
dropped until the entire following month has also elapsed — expiry instead falls back to
row-level TTL deletion during background merges, which rewrites data rather than just removing a
directory, and is materially more expensive at high span volume than a partition drop.

Daily partitions fix this: every row in a given day's partition expires on the same day, so once
30 days have passed, ClickHouse drops that whole partition as a cheap metadata operation. This is
the standard pattern for a rolling-window retention policy and is the reason this ADR approves the
partitioning change.

TTL expression:

```sql
TTL toDate(start_time) + INTERVAL 30 DAY DELETE
```

`toDate(...)` truncates to day granularity so the TTL boundary aligns exactly with the (now daily)
partition boundary.

### 8. Deduplication semantics

**The logical identity of a span is `(project_id, trace_id, span_id)`.** This triple is what
"the same span" means for idempotency purposes — a retried ingestion request for a span that was
already stored must be recognized as a duplicate of the same logical span, not a new one.

**`ingested_at` is only the `ReplacingMergeTree` version column.** It is the server-receipt
timestamp, and its sole role in deduplication is to tell `ReplacingMergeTree` which physical row to
keep when two rows share the same `ORDER BY` key (which, per §6, includes `project_id`, `trace_id`,
and `span_id`): the row with the greater `ingested_at` survives a merge. `ingested_at` is not part
of the logical identity, and it is not evidence of *what* the span is — a span retried with an
identical payload simply produces a second row with a later `ingested_at`, and the two are
collapsed to one by `ReplacingMergeTree`, keeping the most recently received copy.

**ClickHouse's deduplication is eventual, not immediate.** `ReplacingMergeTree` removes duplicate
rows only as a side effect of background merges, on no fixed schedule. A query issued immediately
after a retried insert can transiently observe both the original and the retried row for the same
`(project_id, trace_id, span_id)` before the next merge runs.

**Read paths that require immediate correctness must account for this explicitly** — using
`FINAL` (forcing merge-time deduplication logic at query time) or an equivalent
`LIMIT 1 BY (project_id, trace_id, span_id)` strategy. A single-trace detail view is the clearest
example: it must not show duplicate spans just because a retry hasn't been merged away yet. Broad
analytical aggregates (counts, cost sums) may tolerate the brief, self-correcting overcounting a
retry can cause between merges, per ADR 002.

**API-level idempotency is out of scope for this ADR and will be addressed when ingestion is
implemented.** `ReplacingMergeTree` provides storage-layer deduplication of retried inserts; it
does not by itself give the ingestion API a way to, for example, return a consistent response for
a retried request before ClickHouse has merged anything, or to reject/short-circuit an obvious
duplicate before insertion. That behavior belongs to the ingestion endpoint's design, not the
storage schema, and is deferred to the ADR/implementation that builds `POST /v1/traces`.

### 9. Retention behavior

- Default retention is **30 days from `start_time`** (client event time, not `ingested_at`),
  enforced via the ClickHouse `TTL` clause in §7.
- This is a single global default for V1. **No plan-based or per-project retention tiers exist
  yet** — every project's telemetry is retained for the same 30 days.
- Configurable retention (e.g. longer retention on a paid plan, or per-project overrides) is an
  explicitly **future capability**, not implemented here, and will require its own follow-up ADR
  when it's built (likely needing a per-row or per-partition retention attribute, since ClickHouse
  TTL is normally expressed against a single table-wide expression).
- TTL deletion runs as part of background merges, not synchronously — there is inherent lag
  between "30 days have elapsed" and physical deletion. 30-day retention should not be treated as
  an instantaneous hard deletion guarantee without also accounting for merge scheduling.
- The `traces` materialized view's target table must carry the same TTL expression (keyed off the
  same `start_time`-derived value) so trace rollups don't outlive their source spans.

### 10. Payload limits

- Maximum `input`: 64 KiB per span.
- Maximum `output`: 64 KiB per span.
- Maximum total serialized span payload: 256 KiB.
- These are enforced by the ingestion layer (not implemented here), which must truncate rather than
  reject where practical, and record the outcome in `input_truncated`/`output_truncated`/
  `attributes_truncated`/`events_truncated` plus `input_size_bytes`/`output_size_bytes` (§2).
- **Interaction between the per-field and total caps**: the 64 KiB caps on `input` and `output`
  apply independently and first (128 KiB combined, worst case). The 256 KiB total is a backstop
  over the entire serialized span — identity/timing/status/LLM columns are small and fixed, so the
  variable budget is effectively `input` + `output` + `attributes` + `events`. If input and output
  are both truncated to their 64 KiB caps and the span is still over 256 KiB, `attributes` is
  truncated next, then `events` — preserving the fields ADR 002 designates highest-value (input,
  output, LLM cost/token columns) ahead of the generic, lower-priority attributes bag.
- **`attributes`/`events` have no dedicated per-field cap** — they are bounded only by whatever
  remains of the 256 KiB total after `input`/`output`. This is deliberate: giving them their own
  fixed ceiling isn't warranted by anything specified so far and would constrain the open-ended
  attribute bag ADR 002 relies on; but leaving them fully unbounded would let a large `attributes`
  payload silently exceed the total budget, or become a path to smuggle large content past the
  `input`/`output` caps. Truncation of `attributes`/`events` to fit the remainder is recorded via
  `attributes_truncated`/`events_truncated`, matching the "never silently lose truncation" rule for
  input/output, though (unlike input/output) their pre-truncation byte size is not separately
  preserved in this V1 schema.

### 11. Privacy considerations

- `input` and `output` are the highest-risk fields — they may carry end-user PII, business data, or
  leaked secrets — and are deliberately nullable/omittable so a customer can send only token counts
  and metadata if required.
- They are structurally isolated from `attributes` (dedicated columns, not bag entries) so a future
  redaction pass can target them specifically without needing to parse the generic bag.
- No automatic PII/secret redaction is implemented in V1; raw prompts/responses are stored as sent,
  bounded only by the payload caps in §10. This is an explicit, accepted V1 gap, not an oversight.
- `project_id` must always be derived server-side from the authenticated API key and never accepted
  from the request payload — this prevents a caller from injecting spans into another tenant's
  project. This remains an ingestion-layer requirement (implemented when `POST /v1/traces` is
  built), not something the storage schema itself can enforce.
- ClickHouse has no equivalent to PostgreSQL row-level security; every telemetry read must be
  scoped to the requesting user's authorized `project_id` set at the application layer.
- 30-day retention (§9) reduces the retained blast radius of the above risks but is not itself a
  redaction mechanism, and — per §9 — is not an instantaneous deletion guarantee, which matters if
  retention is ever relied on for a compliance/erasure commitment rather than pure cost control.

### 12. PostgreSQL vs. ClickHouse boundary

Unchanged from ADR 001/002, restated for completeness:

- **PostgreSQL** holds `users`, `organizations`, `organization_memberships`, `projects`, and
  `api_keys` — low-volume, mutable, relational-integrity-critical data accessed via point lookups
  and small joins, requiring strong ACID consistency.
- **ClickHouse** holds `spans` and the derived `traces` rollup — high-volume, append-only,
  time-ranged/aggregated telemetry.
- The two stores are joined only at the application layer: an ingestion request resolves its API
  key to a `project_id` from PostgreSQL once, and that `project_id` is denormalized onto every span
  row so the trace/span explorer never needs a live cross-database join to render.

### 13. Trace reconstruction

A trace is not stored as an independently ingested or mutated entity — it is the set of all rows
in `spans` sharing a `trace_id` (scoped to a `project_id`). Trace-level fields (start time, overall
duration, status, root span, environment, release) are derived from that span set via a
`traces` materialized view keyed on `trace_id`, triggered incrementally on span insert rather than
computed by a full scan at read time. Trace status is derived, not independently settable:
`error` if any span has `status = error`; else `ok` once the root span
(`parent_span_id IS NULL`) has arrived with a non-null `end_time`; else `unknown`. This ADR does
not define the materialized view's exact ClickHouse definition (e.g. `AggregatingMergeTree` state
functions vs. a `ReplacingMergeTree` summary row) — that is implementation work for when the view
is built, constrained to use the same TTL as `spans` (§9).

### 14. OpenTelemetry mapping

- `trace_id` / `span_id` use OpenTelemetry/W3C-compatible formats: 128-bit hex-encoded as 32
  characters, and 64-bit hex-encoded as 16 characters, respectively — the same format used by the
  W3C Trace Context header and OpenTelemetry SDKs generally, chosen for free interoperability with
  any OpenTelemetry-instrumented tooling a customer already runs.
- `status` follows OpenTelemetry's three-value span status model (`unset`/`ok`/`error`) directly,
  rather than a Vigil-specific vocabulary.
- The ingestion envelope's `resource` object (`sdk.name`, `sdk.version`, `service.name`) mirrors
  OpenTelemetry's `ResourceSpans` shape, and the per-span `resource` column denormalizes the
  relevant identifier from that shared object onto each row.
- `span_type` is Vigil-specific (not an OpenTelemetry field) and is deliberately an open,
  unconstrained string with only a recommended vocabulary, so new instrumentation categories never
  require a schema migration to be ingested.

### 15. Important tradeoffs

- Daily partitioning (vs. the originally-proposed monthly) means more partitions exist at steady
  state (roughly 30-40 live partitions for a 30-day retention window instead of 1-2), which is a
  standard and well-tolerated tradeoff for ClickHouse, made specifically to keep TTL expiry a cheap
  partition drop rather than row-level deletion.
- `ReplacingMergeTree` deduplication is only eventually consistent; every read path must decide
  whether it needs `FINAL`/`LIMIT BY` (immediate correctness, more expensive) or can tolerate
  transient duplicates (cheaper, eventually self-correcting).
- The hybrid schema (LLM fast-path columns + generic `attributes`/`events`) remains coupled to
  "LLM observability is the primary use case," per ADR 002; other span types may eventually need
  their own fast-path columns, which will be a schema change at that point.
- Applying the 256 KiB total cap to `attributes`/`events` without a size-preservation column for
  them (unlike `input`/`output`) means a truncated attributes bag records *that* it was truncated
  but not its original size — an accepted asymmetry for V1, reversible later if needed.
- No redaction is implemented in V1 (§11); this is an accepted, explicit gap, not an oversight.
- API-level idempotency (as distinct from storage-level deduplication) is not addressed by this
  ADR and remains open work for the ingestion implementation.

## Reasoning

Each individual choice's reasoning is inlined under its decision above (types, engine, partitioning
change, payload-limit interaction). At a high level: this ADR resolves every question ADR 002 left
open for "implementation time" by choosing the most standard ClickHouse idiom available for each
field's actual shape (fixed-width IDs, closed enums where the vocabulary truly is closed, `Map`/
`Nested` for open-ended bags, `Decimal` for money), and revises only the one ADR 002 proposal that
a concrete retention number (30 days, not a month multiple) made incorrect: monthly partitioning.

## Tradeoffs

See "Important tradeoffs" (§15) above; not duplicated here.

## Consequences

- The `spans` table must be created exactly as specified in §2–§7 when ClickHouse is provisioned,
  unless a further follow-up ADR revises it.
- ADR 002 has been amended in place to reflect `PARTITION BY toDate(start_time)` in place of the
  originally-proposed `toYYYYMM(start_time)`; no other ADR 002 decision changes.
- The ingestion API implementation (separate, later work) must: derive `project_id` server-side
  from the authenticated API key; enforce the payload caps in §10 with truncation, not rejection,
  where practical; and set the `*_truncated`/`*_size_bytes` columns accordingly.
- The `traces` materialized view implementation (separate, later work) must use the same TTL as
  `spans` and must not be treated as an independently-mutated entity.
- API-level idempotency, redaction, OTLP-compatible ingestion, and configurable/per-plan retention
  tiers remain open, per ADR 002, and should each be recorded in their own follow-up ADR when
  implemented.
