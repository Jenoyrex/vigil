# Vigil

Vigil is an open-source LLM observability and evaluation platform.

This repository is a polyglot monorepo currently in the initial scaffolding stage. No application
features have been implemented yet.

## Repository layout

```
apps/
  api/                FastAPI backend
  dashboard/          Next.js + TypeScript dashboard

packages/
  sdk-python/          Python SDK
  sdk-typescript/       TypeScript SDK

services/
  worker/              Asynchronous background processing
  evaluator/           AI evaluation engine

infrastructure/        Docker/deployment configuration (not yet populated)

docs/
  architecture/         Architecture documentation
  api/                   API documentation
  decisions/             Architecture decision records (ADRs)

tests/                  Shared/integration tests
examples/               Example AI applications instrumented with Vigil
```

## Tooling

- **TypeScript ecosystem** (`apps/dashboard`, `packages/sdk-typescript`): managed as a
  [pnpm workspace](./pnpm-workspace.yaml). No other JS workspace/monorepo tool (npm workspaces,
  Turborepo, Nx) is used.
- **Python ecosystem** (`apps/api`, `packages/sdk-python`, `services/worker`,
  `services/evaluator`): each is an independent Python project managed with
  [uv](https://github.com/astral-sh/uv), with its own `pyproject.toml`. There is no shared Python
  workspace or Poetry usage.

See [`docs/decisions/001-system-architecture.md`](./docs/decisions/001-system-architecture.md) for
the reasoning behind these choices.

## Status

This repository currently contains scaffolding only: directory structure, workspace
configuration, and architecture documentation. Application code, database models, Docker
configuration, and CI workflows have not been added yet.
