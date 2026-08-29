# 1. System Architecture

- Status: Accepted
- Date: 2026-08-29

## Context

Vigil is a new open-source LLM observability and evaluation platform. Before any application
code is written, we need to settle the repository's structural shape: how many languages/runtimes
it spans, how dependencies are managed within each, and where service boundaries lie. Getting this
wrong early is expensive to unwind once application code, CI, and deployment tooling depend on it.

Vigil's product surface naturally spans multiple runtimes: a Python backend and evaluation
pipeline, a TypeScript/Next.js dashboard, and SDKs in both Python and TypeScript for instrumenting
customer applications. It also spans multiple deployable units: a request-serving API, an
asynchronous worker, and an evaluation engine, each with different scaling and resource profiles.

## Decision

We adopt the following architecture for the repository:

1. **Vigil is a polyglot monorepo.** All components — API, dashboard, SDKs, background services,
   infrastructure config, docs, tests, and examples — live in a single repository, organized by
   top-level purpose (`apps/`, `packages/`, `services/`, `infrastructure/`, `docs/`, `tests/`,
   `examples/`).

2. **pnpm manages the TypeScript workspace.** `apps/dashboard` and `packages/sdk-typescript` are
   declared as a single pnpm workspace via a root `pnpm-workspace.yaml`. No other JavaScript
   workspace or build-orchestration tool (npm workspaces, Yarn workspaces, Turborepo, Nx) is used.

3. **uv manages Python projects independently.** `apps/api`, `packages/sdk-python`,
   `services/worker`, and `services/evaluator` are each a standalone Python project with its own
   `pyproject.toml`, managed with uv. There is no Python workspace, no shared root
   `pyproject.toml`, and no use of Poetry.

4. **SDKs are separate client-facing packages.** `packages/sdk-python` and
   `packages/sdk-typescript` are published, client-facing libraries. They must not import from or
   depend on internal implementation code in `apps/api`, `services/worker`, or
   `services/evaluator`.

5. **API, worker, and evaluator have separate service boundaries.** `apps/api`,
   `services/worker`, and `services/evaluator` are independently deployable Python services, each
   with their own dependencies and lifecycle. They do not share a common runtime process.

6. **We are deliberately not creating a shared backend core package yet.** No `packages/core` or
   equivalent internal shared package exists at this stage. Any code shared between the API,
   worker, and evaluator will be duplicated for now rather than centralized prematurely.

7. **PostgreSQL, Redis, and ClickHouse will be introduced when their corresponding features are
   implemented, rather than provisioned upfront.** No database, cache, or analytics store is
   configured as part of this initial scaffolding.

## Reasoning

- A monorepo keeps cross-cutting changes (e.g. an SDK field renamed, a dashboard type updated to
  match) reviewable in a single PR, and gives the project one source of truth for architecture
  decisions, tests, and examples, without requiring cross-repo release coordination this early.
- pnpm and uv are each the natural, minimal tool for their ecosystem: pnpm workspaces handle the
  dashboard/TS-SDK relationship (shared types, local linking) without pulling in a build
  orchestrator; uv gives each Python project fast, reproducible dependency resolution without
  requiring them to share a lockfile or virtual environment.
- Keeping Python projects independent (rather than a uv workspace) matches the services' intended
  independent deployability: the API, worker, and evaluator can each pin their own dependency
  versions and be containerized and released separately without affecting one another.
- Treating the SDKs as strictly client-facing enforces the boundary that they are things customers
  install into their own applications — coupling them to internal implementation details would
  leak internal APIs into a public surface and make internal refactors breaking changes for SDK
  consumers.
- Not building a shared core package yet avoids designing an abstraction before we know what, if
  anything, actually needs to be shared between the API, worker, and evaluator. A premature shared
  package tends to either become a dumping ground or force incompatible services into the same
  shape.
- Deferring PostgreSQL, Redis, and ClickHouse avoids committing to schemas, connection patterns,
  and infrastructure before the features that need them are designed, and keeps this initial setup
  free of dependencies that would otherwise sit unused.

## Tradeoffs

- A monorepo means all consumers see the full repository history and directory tree, and CI will
  eventually need path-based filtering to avoid running every service's checks on every change.
- Using two independent workspace tools (pnpm for TS, uv per-project for Python) means there is no
  single command that builds/tests the entire repository yet; that orchestration is deferred along
  with CI.
- Independent Python projects (rather than a uv workspace) mean shared dependency versions (e.g. a
  common Pydantic version) are not enforced automatically and could drift between the API, worker,
  and evaluator.
- No shared core package means near-term duplication is likely if the API, worker, and evaluator
  end up needing the same internal logic (e.g. shared data models); this is an accepted, deliberate
  cost in exchange for not guessing at the wrong abstraction.
- Deferring datastore selection/config means some architectural questions (e.g. exact ClickHouse
  schema for event ingestion) remain open and will need their own decisions later.

## Consequences

- New TypeScript packages that belong to the dashboard/SDK ecosystem should be added to
  `pnpm-workspace.yaml`; new Python services or packages should be created as their own directory
  with an independent `pyproject.toml`, not added to a shared workspace.
- Any code that appears to be needed by more than one of `apps/api`, `services/worker`, and
  `services/evaluator` should prompt a revisit of decision 6 (introducing a shared core package)
  rather than being informally copy-pasted indefinitely.
- Contributors must not import internal API/worker/evaluator modules from either SDK package; this
  boundary should be enforced by code review until tooling (e.g. import linting) is introduced.
- Database, cache, and analytics store selection remains open; follow-up ADRs should record the
  decision at the point each is introduced (e.g. "Introduce PostgreSQL for X").
- This ADR does not cover CI, Docker/deployment configuration, or database schema design; those are
  intentionally out of scope for this initial architecture decision and will be addressed in
  separate, later work.
