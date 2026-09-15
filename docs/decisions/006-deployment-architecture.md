# 6. Deployment Architecture

- Status: Accepted
- Date: 2026-09-12

## Context

By the end of Phase 4A (worker/evaluator reliability hardening), Vigil had no deployment
artifacts at all: no Dockerfiles anywhere in the repository, and
`infrastructure/docker-compose.yml` provisioned only the two datastores (PostgreSQL,
ClickHouse) for local development. None of `apps/api`, `apps/dashboard`, `services/worker`'s
runtime loop, or `services/worker`'s poller could be built into a container or run outside a
developer's local `uv`/`pnpm` environment. ADR 001 (decision 5) established that API, worker, and
evaluator are separate, independently deployable service boundaries, and explicitly deferred
Docker/deployment configuration to later work (ADR 001, Consequences). This ADR is that later
work: Phase 4B, "Deployment."

The mandate for Phase 4B was the smallest production-quality deployment architecture that makes
the complete platform deployable, without redesigning application architecture, introducing new
infrastructure components (Redis, Kafka, Kubernetes), or solving unrelated production-readiness
gaps (rate limiting, CI/CD, backup/restore, structured logging, TLS termination) that belong to
later phases.

## Decision

1. **Four new Docker images, not six.** The platform's six logical units (API, dashboard, worker
   runtime, worker poller, PostgreSQL, ClickHouse) map to four new application images plus the two
   existing, unchanged datastore images:
   - `apps/api/Dockerfile` -- the FastAPI/uvicorn service.
   - `apps/dashboard/Dockerfile` -- the Next.js dashboard.
   - `services/worker/Dockerfile` -- **one** image serving both `python -m worker` (the
     claim/dispatch/reap runtime) and `python -m worker.poller_main` (the ClickHouse-scanning job
     creation poller), selected at run time via the container `command:`, not by building two
     images.
   - PostgreSQL (`postgres:16-alpine`) and ClickHouse (`clickhouse/clickhouse-server:24.8-alpine`)
     are unchanged from `infrastructure/docker-compose.yml`.

2. **Worker runtime and poller share one image.** Both entrypoints live in the same Python package
   (`worker`, per `services/worker/pyproject.toml`'s `[tool.hatch.build.targets.wheel] packages =
   ["worker"]`) and the same flat, unconditional dependency list, including the heavy
   `vigil-evaluator[embedding]` extra. Splitting them into two images would install the identical
   full dependency set twice for no functional benefit: the poller never calls
   `EvaluatorRegistry.get(...)` (only `.registered_keys()`), so it never loads the embedding model
   regardless of which image it ships in. ADR 001 decision 5 also does not name "poller" as a
   separate deployable service boundary -- only API, worker, and evaluator -- so treating it as
   one more independent image would introduce a service boundary the architecture doesn't
   recognize.

3. **`services/evaluator` is never its own image.** It is a local path dependency of
   `services/worker` (`[tool.uv.sources] vigil-evaluator = { path = "../evaluator" }`), imported as
   a library inside the worker process, consistent with ADR 004/005's "one process, imported as a
   library" framing. Its source is required in the worker image's build context but it is never
   run as a standalone container.

4. **Docker Compose, not Kubernetes.** Evaluated explicitly and rejected: the platform is six
   units, single-region, with no existing Helm/Kubernetes manifests, no autoscaling requirement
   anywhere in the repository, and Phase 4A's one hard deployment-time requirement -- an external
   process supervisor to restart the worker after its bounded-orphan self-retirement -- is fully
   satisfiable by Compose's `restart:` policy. Kubernetes would add manifests, probe
   configuration, an ingress choice, and secret-management integration with no corresponding
   requirement driving it.

5. **A separate production Compose file, not an extension of the dev one.**
   `infrastructure/docker-compose.prod.yml` is new and independent;
   `infrastructure/docker-compose.yml` (local development datastores) is unchanged and not
   overridden or layered via `-f` chaining. This is deliberate: the two files diverge on port
   exposure (see decision 8) and would otherwise risk local dev workflow silently picking up
   production-shaped network restrictions, or a production deploy silently picking up dev-only
   host port publishing, through a chaining mistake.

   **Explicit project-name isolation (`name: vigil-prod`).** Both Compose files live in the same
   `infrastructure/` directory, and both declare a `postgres`/`clickhouse` service backed by
   identically-named volumes (`vigil_postgres_data`/`vigil_clickhouse_data`, carried over from the
   dev file so the schema-provisioning ClickHouse init scripts stay identical between the two --
   see decision 8's sibling reasoning). Without an explicit project name, Compose derives one from
   the current directory basename for *both* files alike (`infrastructure`), which means their
   containers, default volume names, and network would all resolve to the exact same names --
   `docker compose -f infrastructure/docker-compose.prod.yml up` with no other flags would then
   recreate the dev stack's own `postgres`/`clickhouse` containers against production
   configuration and placeholder credentials. This is not a hypothetical: it happened once during
   this ADR's own review validation, before this fix. `docker-compose.prod.yml` now pins its
   project identity with a top-level `name: vigil-prod`, the Compose Specification's own mechanism
   for this (precedence: `-p` flag > `COMPOSE_PROJECT_NAME` env var > this field > directory
   basename) -- deliberately chosen over `COMPOSE_PROJECT_NAME` in
   `infrastructure/.env.production` because a value committed directly in the compose file applies
   unconditionally, with nothing an operator must remember to set, and is immediately visible to
   anyone reading the file. `infrastructure/docker-compose.yml` is intentionally left with no
   `name:` of its own (still defaulting to `infrastructure`) -- this fix only had to change one
   side of the collision, and changing the dev file risks nothing but has no corresponding benefit.
   The practical effect: `postgres`/`clickhouse`/`api`/`dashboard`/`worker`/`poller`/`migrate`
   containers are named `vigil-prod-<service>-1`, the two volumes resolve to
   `vigil-prod_vigil_postgres_data` and `vigil-prod_vigil_clickhouse_data`, and the network resolves
   to `vigil-prod_default` -- all disjoint from the dev stack's `infrastructure_*`-prefixed
   equivalents, and reachable with a bare `docker compose -f infrastructure/docker-compose.prod.yml
   ...` invocation, no `-p` flag required. One operational consequence worth stating plainly: this
   renames what a production deploy resolves to relative to any environment that already ran
   `docker-compose.prod.yml` before this fix existed -- such a deploy's prior `infrastructure_*`
   containers/volumes are simply orphaned (never touched or deleted automatically) rather than
   reused, and would need manual cleanup if no longer wanted. Since Phase 4B has not yet shipped a
   real production deployment, no environment is affected by this in practice.

6. **Worker image build context: a named additional context, not the repository root.** The
   worker's local path dependency on `services/evaluator` means its Docker build needs both
   `services/worker` and `services/evaluator` source trees. Rather than using the whole repository
   as the build context (which would make `services/worker/.dockerignore` inapplicable, since
   Docker only honors a `.dockerignore` at the context root), the build's primary context is
   `services/worker` itself, with `services/evaluator` supplied as a separate, named BuildKit
   additional context called `evaluator` (`docker build --build-context
   evaluator=services/evaluator`, or Compose's `build.additional_contexts`). The Dockerfile's
   `WORKDIR` is set to `/build/worker` and the evaluator context is copied to the sibling path
   `/build/evaluator`, mirroring the exact `../evaluator` relative relationship the committed
   `uv.lock` resolves (`source = { directory = "../evaluator" }`) so the frozen install resolves
   identically to a developer's local checkout. The dashboard image uses the same technique for
   its own cross-directory need (the pnpm workspace lockfile lives at the repository root, one
   level above `apps/dashboard`): primary context `apps/dashboard`, named additional context
   `workspace` pointing at the repository root, supplying only `pnpm-lock.yaml` and
   `pnpm-workspace.yaml`.

7. **One-shot migration service, not migration-on-API-startup.** A dedicated `migrate` Compose
   service runs `alembic upgrade head` to completion using the unmodified `api` image, gated
   behind PostgreSQL being healthy. `api` itself depends on `migrate`'s `service_completed_successfully`
   condition. This avoids baking a schema migration into every `api` container's startup command,
   which would risk concurrent, racing `alembic upgrade head` invocations if `api` is ever scaled
   to multiple replicas. `apps/api/alembic/env.py` always sources `sqlalchemy.url` from
   `app.config.settings.database_url`, so this reuses the exact same environment-driven
   configuration as `api` itself with no migration-specific setup required.

8. **No datastore ports published to the host in production.** Unlike
   `infrastructure/docker-compose.yml` (which publishes PostgreSQL on `5434` and ClickHouse on
   `8123`/`9000` for local tooling access), `docker-compose.prod.yml` gives `postgres` and
   `clickhouse` no `ports:` entry at all -- reachable only from other containers on the Compose
   network. `api` is published on host port `8000`, `dashboard` on host port `3000` (both fixed,
   matching local development's own default ports). `worker` and `poller` publish no ports, since
   neither runs an HTTP server.

9. **Health/readiness strategy follows the existing endpoints exactly, unmodified.** `api`'s
   container `HEALTHCHECK` targets `GET /ready` (checks ClickHouse then PostgreSQL, 503 on
   failure) rather than `GET /health` (pure liveness, always 200) -- deliberately, so an unhealthy
   container accurately reflects "can't reach a backing store." `dashboard` and `poller` both gate
   their startup on this via `depends_on: api: condition: service_healthy`, since the poller calls
   `api`'s internal job-creation endpoint over HTTP (`worker/poller.py`) and the dashboard proxies
   every request to `api`. `worker` (the claim/dispatch/reap runtime loop) deliberately has no such
   dependency on `api` -- it talks only to the datastores directly (`postgres`, `clickhouse`) and
   never calls `api` at all, so gating it on `api`'s health would add a startup dependency with no
   corresponding runtime one. `dashboard`'s healthcheck is a plain HTTP reachability check against
   `/`, since Next.js has no equivalent readiness concept of its own. `worker` and `poller` have no
   *HTTP* protocol for Docker to probe (neither runs a server), but as of Phase 4D both do have a
   container `HEALTHCHECK`, via a heartbeat file rather than an HTTP endpoint -- see "Known
   limitations" for why an HTTP server was deliberately not added solely to satisfy this, and what
   the heartbeat approach actually proves. Restart-on-crash is still enforced by the restart policy
   below regardless of what the healthcheck reports.

10. **`restart: unless-stopped` for `worker` and `poller`.** This is the external process
    supervisor that Phase 4A's `WorkerRuntime` bounded-orphan self-retirement
    (`request_stop()`/`_check_orphaned_evaluator_threshold()` in
    `services/worker/worker/runtime.py`) explicitly, and by design, depends on to bring a fresh,
    zero-orphan process back up after this one retires itself -- Phase 4A's own settings docstring
    (`max_orphaned_evaluator_threads`) states recovery "depends entirely on an external process
    supervisor (systemd/Docker/Kubernetes restart policy)." `unless-stopped` is chosen over a
    bounded `on-failure:N` because self-retirement is a clean, intentional exit (not necessarily a
    non-zero one), and the policy must restart the process unconditionally in both the
    self-retirement case and a genuine crash -- treating both uniformly is correct here, since
    Phase 4A already treats self-retirement as an expected, bounded event rather than a terminal
    failure state.

11. **All secrets flow through environment variables only, never baked into an image layer.**
    `infrastructure/.env.production.example` documents every variable every production service
    needs, with placeholder (`CHANGE_ME`) values only -- unlike `infrastructure/.env.example`
    (local development), there are no working insecure defaults. The one shared secret in the
    system, the internal service token (`VIGIL_API_INTERNAL_SERVICE_TOKEN` /
    `VIGIL_WORKER_INTERNAL_SERVICE_TOKEN`), must be set to the identical value on both `api` and
    `worker`/`poller` -- this is called out explicitly in the template, since it is a
    shared-secret pattern rather than the more common per-service-credential pattern used
    everywhere else in this file.

12. **Non-root containers.** Every application image (`api`, `dashboard`, `worker`, which also
    serves `poller`) creates and runs as a dedicated non-root user (`vigil`, uid/gid 1000). None
    requires a privileged operation, a root-owned bind mount, or binding to a port below 1024.
    One real blocker was found and fixed during review: `vigil` is created with
    `--no-create-home`, so it has no writable `$HOME`, and
    `services/evaluator/app/embedding_relevance.py`'s `EmbeddingRelevanceEvaluator` (used by the
    `relevance_embedding` evaluator, registered by default and reachable through the public
    evaluator config API) defaults its `fastembed` model-weights cache to a path under
    `Path.home()` -- a permanent permission failure the moment `relevance_embedding` runs in the
    worker or poller container. Empirically exercising the real download path surfaced a second,
    deeper instance of the same root cause: `huggingface_hub`'s `xet` accelerated-download backend
    writes its own log files straight to `$HOME/.cache/huggingface/xet/logs`, independent of
    fastembed's `cache_dir` argument, so pointing only the model cache elsewhere was not
    sufficient. `services/worker/Dockerfile` now pre-creates a worker-owned, writable cache
    directory (`/app/.cache/vigil-evaluator/fastembed`), `chown`s the whole `/app/.cache` tree to
    `vigil` while still root, sets `VIGIL_EVALUATOR_EMBEDDING_CACHE_DIR` (the evaluator's own
    environment-variable override, already supported before this ADR) to that path, and -- to close
    off ancillary-library writes like `xet`'s without having to chase each one individually --
    repoints `HOME` itself at `/app`, which the same `chown` already covers. Both the `worker` and
    `poller` services -- which share this one image -- pick all of this up automatically via the
    image's `ENV`, with no Compose-level change and no filesystem permissions wider than that one
    directory tree. Verified empirically inside the built image: the process runs as uid 1000
    (`vigil`), `VIGIL_EVALUATOR_EMBEDDING_CACHE_DIR` is visible to it, the cache directory is
    writable, and `EmbeddingRelevanceEvaluator` successfully downloads the model and produces a
    result with no permission errors.

## Rollback approach

Compose-native, not automated: `git checkout <previous-tag-or-commit>` followed by `docker compose
-f infrastructure/docker-compose.prod.yml --env-file infrastructure/.env.production up -d --build`
rebuilds and restarts every application service from the prior commit's Dockerfiles and source.
Database rollback uses Alembic's existing, unmodified `downgrade` capability, invoked manually by
an operator -- this phase does not automate migration rollback, only ensures forward migration
runs correctly as a container. No blue-green deployment, canary, or automated rollback tooling is
introduced; that is CI/CD-adjacent scope explicitly deferred past Phase 4B.

## Known limitations

- **~~No I/O timeout on PostgreSQL calls~~ -- resolved, Phase 4D.** `services/worker/worker
  /postgres/client.py`'s `get_connection()` now sets both libpq's `connect_timeout` and
  PostgreSQL's own server-side `statement_timeout` from the new `database_timeout_seconds`
  setting (mirroring `worker/clickhouse/client.py`'s pre-existing `connect_timeout`/
  `send_receive_timeout` pattern) -- a stuck PostgreSQL call can no longer block a worker thread
  indefinitely. One residual, explicitly accepted gap remains: a network partition occurring
  *after* a connection is established, where the server's own cancellation response never
  arrives, could in principle still exceed this bound -- see that module's own docstring.
- **~~No HTTP healthcheck for `worker`/`poller`~~ -- resolved differently, Phase 4D.** Rather than
  adding an HTTP server to either process solely to satisfy a healthcheck (new exposed network
  surface for zero other benefit), both `worker/runtime.py`'s `WorkerRuntime.run` and
  `worker/poller.py`'s `Poller.run` now touch a heartbeat file (`worker/heartbeat.py`) once per
  completed loop iteration, and this image's `HEALTHCHECK` instruction checks that file's
  freshness instead. Because every I/O call inside one iteration is now individually time-bounded
  (the PostgreSQL fix above, `clickhouse_timeout_seconds`, and Phase 4A's evaluator-call/
  evaluator-construction timeouts), a heartbeat that stops refreshing for longer than a generous,
  configurable bound (`heartbeat_stale_seconds`, default 180s) is a genuine "this loop is stuck,
  not just busy" signal, not merely "the process exists." A worker stuck in a way that never
  triggers the bounded-orphan self-retirement Phase 4A already added (a scenario meaningfully
  narrowed by this phase's own PostgreSQL fix) will now be caught by this healthcheck instead of
  appearing indefinitely healthy to Docker.
- **~~No org/project/API-key provisioning endpoint~~ -- resolved, Phase 4D (F3).**
  `POST /v1/provisioning/bootstrap` is now the production, HTTP-based equivalent of
  `apps/api/scripts/seed_local_api_key.py` -- deliberately bootstrap provisioning for a single
  trusted operator, not a public signup system (this codebase still has no user-facing
  authentication of any kind). Protected by a dedicated, environment-configured secret
  (`VIGIL_API_BOOTSTRAP_SECRET`, empty/disabled by default, compared with
  `hmac.compare_digest`, structurally identical to `get_internal_service_auth`'s existing
  internal-worker-token pattern -- a customer `vgl_*` API key can never satisfy it) and an
  IP-keyed in-process rate limiter (no Redis). Succeeds at most once per deployment, enforced by
  the database in two layers, not an application-level flag: an `organizations`-non-empty check
  runs first and alone refuses every repeat call (including one where only the audit/diagnostic
  marker row described below was deleted -- see the next sentence), and an atomic,
  conflict-checked insert against a new single-row `provisioning_bootstrap` table (mirroring
  `evaluation_poller_checkpoint`'s existing fixed-string-primary-key singleton idiom) is what
  makes a *successful* run safe under concurrent requests specifically. There is deliberately no
  "delete one row to re-enable bootstrap" path -- re-enabling it requires a full, destructive
  teardown of the organization/project/API key the original run created, documented as exactly
  that (operator-level, disposable-environment-only) in `apps/api/README.md`'s "Provisioning"
  section, which also has the full operator workflow; see `app/services/provisioning.py`'s module
  docstring for the concurrency/atomicity argument in full.
- **No TLS termination or reverse proxy.** `docker-compose.prod.yml` exposes `api` and `dashboard`
  as plain HTTP on the host. TLS termination is assumed to be handled by infrastructure the
  operator already has in front of this stack (a load balancer, an existing reverse proxy) --
  introducing one here would be new infrastructure beyond Phase 4B's approved scope.
- **No CI/CD, registry, or automated image-publishing pipeline.** Images are built locally by
  `docker compose ... up --build`; pushing to a registry and referencing images by tag is a
  natural next step but explicitly deferred, along with backup/restore tooling.
- **~~No structured logging~~ -- resolved, Phase 4D (F4).** `apps/api` and `services/worker` each
  gained a small, stdlib-only `logging_config.py` (deliberately duplicated, not shared, per ADR 001
  decision 6 -- these are two independently deployable services) that replaces
  `logging.basicConfig(level=logging.INFO)` with one JSON object per line on stdout: `timestamp`,
  `level`, `service` (`"api"` / `"worker"` / `"poller"`), `logger`, `message`, and whatever
  identifiers a call site's own `extra={...}` supplies (`request_id`, `project_id`, `worker_id`,
  `job_id`, `evaluator_name`, etc.) as genuine top-level fields, never folded into the message
  string. `apps/api` additionally binds one `request_id` per HTTP request
  (`app.middleware.RequestIdMiddleware`) via a contextvar, so every log line anywhere in that
  request's call stack -- including deep, request-agnostic helpers like
  `app/clickhouse/query_common.py` -- carries it automatically, with no per-call-site plumbing.
  `services/worker` does the same with `worker_id` via explicit `extra={...}` at each call site
  instead of a contextvar, since job execution runs on a plain `ThreadPoolExecutor`
  (`worker/dispatcher.py`) that does not propagate one into pool worker threads. No new dependency
  in either service, no remote logging backend, and no OpenTelemetry -- both existing Dockerfiles'
  `PYTHONUNBUFFERED=1` already made stdout a reliable log sink for Docker's default `json-file`
  driver; this only changed what gets written to it. `VIGIL_API_LOG_LEVEL`/`VIGIL_WORKER_LOG_LEVEL`
  (both default `INFO`) control verbosity; an invalid value fails at process start. See
  `apps/api/README.md` and `services/worker/README.md`'s own "Logging" sections.

## Consequences

- Any new environment variable added to `apps/api/app/config.py` or `services/worker/worker/config.py`
  must also be added to `infrastructure/.env.production.example`, or a production deployment will
  fail at container startup with a missing-setting error (`pydantic-settings` has no default for
  fields declared without one, such as `internal_service_token`).
- Building the worker or dashboard image requires the named additional-context flag/Compose key
  (`--build-context evaluator=services/evaluator` / `additional_contexts:`); a plain `docker build
  services/worker` (or `apps/dashboard`) without it will fail with an unresolvable path dependency
  or missing lockfile, respectively. This is documented at the top of each Dockerfile.
- Future work reintroducing rate limiting, CI/CD, TLS termination, or backup/restore should treat
  this ADR's Compose topology as the base to extend, not redesign, unless a genuine architectural
  reason emerges.
