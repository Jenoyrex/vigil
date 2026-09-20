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

    **The embedding model is baked into the image; production never downloads it.** The
    paragraph above records the state when this decision was first written, when the model was
    fetched from Hugging Face on first use, so every worker container *recreation* (every deploy)
    re-downloaded about 65 MB, and a host without outbound access could stall each
    `relevance_embedding` job for the full `evaluator_init_timeout_seconds`. `relevance_embedding`
    is supported V1 functionality, so `services/worker/Dockerfile` now downloads the model at
    build time, as the `vigil` user, into that same cache directory by constructing the real
    `EmbeddingRelevanceEvaluator` (same code path, model name and cache variable the running worker
    uses). The build fails unless the model file fastembed's own registry names for it
    (`qdrant/bge-small-en-v1.5-onnx-q`'s `model_optimized.onnx`) is present and non-trivially sized,
    and unless a second construction with `HF_HUB_OFFLINE=1` succeeds from that cache alone. The
    runtime image then sets `HF_HUB_OFFLINE=1`, so inference never contacts Hugging Face and a
    missing model fails immediately instead of stalling on retries. Consequences: the model cache
    is part of the immutable image, so the **image tag determines the bundled model snapshot**
    (the repository's latest revision at build time); rolling back an image rolls back the model.
    **Do not mount a volume over `/app/.cache/vigil-evaluator/fastembed`** -- it would hide the
    baked model. The download layer sits before the application `COPY`, so application-only
    changes reuse it. Model revision pinning is intentionally deferred (fastembed 0.8's public
    constructor has no `revision` argument; `specific_model_path` would be the route).

13. **CI/CD publish and manual protected deploy (Phase 4D, F7).** `.github/workflows/cd.yml` is new;
    `.github/workflows/ci.yml` is unchanged and remains validation-only -- the two are independent
    workflows, not chained.

    - **Registry: GHCR, `GITHUB_TOKEN`, no long-lived PAT.** `ghcr.io/<owner>/vigil-{api,dashboard,
      worker}` (the worker image still serves both `worker` and `poller`, per decision 2, unchanged).
      `docker/login-action@v3` authenticates the publish job with the workflow's own `GITHUB_TOKEN`
      (`packages: write`), which is sufficient for pushing -- no registry PAT is stored anywhere.
      **GHCR packages created from a workflow's `GITHUB_TOKEN` default to private regardless of the
      repository's own visibility.** Since this repository is public and GHCR is the default
      registry choice here, an operator must, after the first successful publish of each of the
      three packages, open that package's own Settings (github.com -> the package's page, not the
      repository's) and change visibility to Public -- otherwise `docker compose pull` on the
      deploy host (which has no registry credentials of its own; see below) will get `denied`
      rather than the image.
    - **Tags: `docker/metadata-action@v5`, not hand-rolled.** Every image is tagged
      `sha-<full-40-character-commit-sha>` (`type=sha,format=long,prefix=sha-`) -- the *only* tag
      `cd.yml`'s `deploy` job will ever act on, enforced by a regex check
      (`^sha-[0-9a-f]{40}$`) before any SSH connection is made. The sanitized branch name and,
      master only, `latest` are also published as convenience/debugging tags, but neither is ever
      an accepted `deploy` input -- there is no code path from "push a branch" or "the `latest`
      tag" to a running production container.
    - **Publish trigger and the CI gate.** `publish` runs on push to `master` and on
      `workflow_dispatch` with no `image_tag` given. `ci.yml` triggers on that same `push`, as an
      independent workflow -- there is no `needs:` across separate workflow files (not expressible;
      GitHub Actions dependency graphs are per-workflow), and merely asserting "`ci.yml` exists" is
      not a real gate. Duplicating `ci.yml`'s full Postgres/ClickHouse/dashboard-build suite inside
      `cd.yml` was rejected as needlessly expensive to run twice per push. Instead, a `ci_gate` job
      polls the Actions REST API (`gh api repos/<repo>/actions/workflows/ci.yml/runs?head_sha=<sha>`)
      for *that exact commit's* `ci.yml` run and blocks `publish` until it reports `conclusion:
      success` (bounded: ~22.5 minutes, then fails closed) -- a real, per-commit dependency on CI's
      actual outcome, not its existence. `ci_gate` (and therefore `publish`) is skipped entirely
      when `image_tag` is supplied to `workflow_dispatch`, since a rollback-only run publishes
      nothing new for CI to have validated.
    - **Deploy: `workflow_dispatch` only, the `production` Environment, non-overlapping.** `deploy`
      is skipped unconditionally for every `push` event, however it completes -- the only trigger
      that can run it is an operator's manual dispatch. It runs under the `production` GitHub
      Environment (required reviewers / wait timers, if configured there, apply on top of
      everything below) and `concurrency: {group: deploy-production, cancel-in-progress: false}`,
      so a second dispatch queues rather than racing or cancelling one already in progress. Leaving
      `image_tag` blank resolves it to `sha-<the sha this dispatch is running against>` (the exact
      commit the operator picked in the "Run workflow" branch selector, which is as close as
      `workflow_dispatch` semantics get to "default to current master" -- there is no supported way
      to make an input's `default:` itself a dynamic expression); supplying an earlier `sha-<...>`
      tag deploys that instead, with `ci_gate`/`publish` both skipped -- **no rebuild**, exactly the
      rollback mechanism this ADR originally promised.
    - **No fabricated deployment host.** This repository names no server. `deploy` fails closed,
      before opening any SSH connection, if the `production` Environment is missing any of four
      required secrets: `DEPLOY_HOST`, `DEPLOY_SSH_USER`, `DEPLOY_SSH_KEY` (a private key whose
      public half is authorized on that host for that user), `DEPLOY_SSH_KNOWN_HOSTS` (that host's
      pinned public host key(s), in the same format `ssh-keyscan` prints -- pin it once, out of
      band, and never fetch it automatically). These are GitHub Environment secrets, entered once
      by an operator in repository Settings; none of the four is ever committed. `StrictHostKeyChecking`
      stays at its secure default (`yes`) against the pinned known-hosts file -- never `no`, and
      no secret is ever echoed to the job log. The deploy host only needs `~/vigil/infrastructure`
      to exist as an SSH working directory for the `docker compose` invocations below to run from
      (the same convention this ADR's original "Rollback approach" section, below, already
      assumed) -- `deploy` itself keeps `docker-compose.prod.yml` and `clickhouse/init/` there
      current on every run; see "Deployment configuration travels with the image, from the exact
      same commit" below for how.
    - **Compose consumes registry images without silently rebuilding.** `infrastructure/
      docker-compose.prod.yml`'s five application services now each declare both an `image:` (a
      required variable, `${VIGIL_API_IMAGE:?...}` / `VIGIL_DASHBOARD_IMAGE` / `VIGIL_WORKER_IMAGE`
      -- no default, so it can never silently resolve to nothing or to `latest`) and their existing
      `build:`. Both are genuinely needed: `build:` for local/manual building (unchanged usage),
      `image:` for the deploy path. Getting this combination right required empirical verification,
      not assumption, because it hides a real footgun: with both keys present, `docker compose up`
      -- with **no** flag at all, even `pull_policy: always` -- silently falls back to *building
      from source* the moment a named `image:` tag fails to pull (verified directly against this
      repository's own compose file). The only combination that fails closed instead is `docker
      compose pull` (a separate, standalone command -- this one never builds; it hard-errors if a
      tag is unavailable) followed by `docker compose up --no-build` (which refuses to build even
      if, somehow, the image were still missing). `cd.yml`'s `deploy` job uses exactly that
      sequence and no other. Postgres and ClickHouse are untouched -- still the plain upstream
      `postgres:16-alpine` / `clickhouse/clickhouse-server:24.8-alpine` images, decision 1, never a
      Vigil registry image.
    - **Migration ordering, unchanged dependency graph, real exit codes.** `deploy` runs `docker
      compose pull migrate api dashboard worker poller` (fails closed on any missing tag), then
      `docker compose up --no-build --exit-code-from migrate migrate` -- which, via the *existing*,
      unmodified `depends_on: postgres: condition: service_healthy` (decision 7), also starts and
      waits on `postgres` first, then runs `migrate` to completion and surfaces its real exit code
      as the command's own exit code. A failed migration fails this step and the job stops there --
      `api`/`dashboard`/`worker`/`poller` are never started against an unmigrated or
      partially-migrated schema. Only once that step succeeds does `docker compose up -d --no-build
      api dashboard worker poller` bring up the long-running services, which still wait on their
      own pre-existing `depends_on` conditions (`api` on `migrate`/`postgres`/`clickhouse`;
      `dashboard`/`poller` additionally on `api` being healthy) exactly as decision 9 already
      specifies -- nothing about that graph is bypassed.
    - **Health verification, bounded and real.** After services start, `deploy` retries (30
      attempts, 5s apart, ~2.5 minutes bound) `GET /ready` on `api` (port 8000) and `GET /` on
      `dashboard` (port 3000), from the deploy host itself over `127.0.0.1` -- not from the GitHub
      Actions runner, since this repository makes no assumption that `DEPLOY_HOST` is reachable
      from the public internet. Either endpoint failing to return successfully within the bound
      fails the job. No automatic rollback follows a failed health check in this commit -- the
      previous containers are simply left as `docker compose up` left them (still running the prior
      image, since `up` only replaces a service's container once its new one is healthy/started);
      recovering is the same manual `workflow_dispatch` with the previous `sha-<commit>` tag
      described above.
    - **Rollback assumes schema forward/backward compatibility, not automated downgrade.** Exactly
      as this ADR's original "Rollback approach" section (below) already stated for the pre-4D
      manual rollback, this phase adds no automatic Alembic `downgrade`. Rolling back the *image*
      to an older `sha-<commit>` tag while
      the database has already been migrated forward by a newer commit is only safe if that newer
      commit's migration(s) were expand/contract-compatible with the immediately previous
      application version (additive schema changes the older code simply ignores, not a destructive
      rename/drop the older code depends on). This is a contract every future migration must
      uphold for image rollback to remain safe, not something this ADR or `cd.yml` can enforce
      mechanically.
    - **Deployment configuration travels with the image, from the exact same commit.** Every
      `deploy` run -- a fresh publish or a rollback -- resolves `sha-<commit>` to that 40-character
      commit (the tag *is* the commit; `docker/metadata-action`'s `type=sha,format=long` tag is
      literally `sha-` followed by the commit it was built from, so no separate lookup is needed),
      then runs `actions/checkout@v4` with `ref: <that commit>` on the runner -- never the
      branch/HEAD this workflow happened to trigger from. From that exact checkout, a "Sync
      deployment configuration to host" step transfers only two paths to the deploy host, over the
      same pinned SSH connection used everywhere else in this job:
      `infrastructure/docker-compose.prod.yml` to `~/vigil/infrastructure/docker-compose.prod.yml`,
      and every file under `infrastructure/clickhouse/init/` to
      `~/vigil/infrastructure/clickhouse/init/`. This closes the gap the original version of this
      decision left open: Commit A's image is now always paired with Commit A's Compose file and
      Commit A's ClickHouse init scripts, whether A is the commit just published or an operator
      rolling back to it, so the host can never run a newer/older image against a mismatched
      Compose file. Two things about *how* it transfers are deliberate, not incidental:
      - The remote `clickhouse/init/` directory is `rm -rf` then recreated before anything is
        copied into it. This was verified empirically to matter, not assumed: against a real sshd,
        `scp -r` of a fresh local `init/` onto an already-populated remote `clickhouse/init/`
        merged in without ever clearing what was already there, so a script renamed or removed
        since the previous deploy survived as a stale leftover the deploy silently kept running.
        `rm -rf` before every sync is what makes the destination always reflect exactly the
        checked-out commit's `clickhouse/init/`, nothing older mixed in. The Compose file, copied
        to a single named destination path, is simply overwritten in place and has no equivalent
        staleness case.
      - Each `clickhouse/init/` file is `scp`'d individually into that freshly-created directory
        (no `-r`, no bare directory argument), landing directly at
        `~/vigil/infrastructure/clickhouse/init/<file>`. This sidesteps relying on any particular
        scp implementation's directory-recursion semantics at all -- which do vary across scp
        versions/protocols and are easy to get subtly wrong -- rather than assuming a specific one;
        verified empirically against a real sshd to land flat, never nested as
        `.../clickhouse/init/init/<file>`.
      `infrastructure/.env.production` is never a source or destination path in this step, or
      anywhere else in `cd.yml` -- it stays host-local and operator-owned, exactly as
      `infrastructure/.env.production.example`'s own comments describe, and this deploy mechanism
      never reads, writes, or overwrites it.
      - **Known limitation: a failed pull after a completed sync leaves the on-disk config ahead of
        what's running.** This sync step runs *before* `docker compose pull` (see the deploy order
        above). If the pull fails -- a bad or not-yet-public image tag, a registry outage -- the
        job stops there and no container is ever touched: whatever was running before this dispatch
        keeps running, unchanged. But `docker-compose.prod.yml` and `clickhouse/init/` on the host
        have already been overwritten with the *attempted* commit's versions, so until the next
        successful deploy or rollback resynchronizes them, the on-disk configuration temporarily
        describes the commit that failed to deploy, not the commit actually running. This is a
        deliberate tradeoff, not an oversight: an atomic (temp-directory-and-swap) config deploy
        would close this gap but adds real complexity for a self-healing, narrow-window failure mode
        that never touches a running container. Not implemented here; revisit only if this actually
        causes an operator incident.

## Rollback approach

**Registry-image rollback (Phase 4D, F7), for any commit with a published `sha-<commit>` tag:**
`workflow_dispatch` on `.github/workflows/cd.yml` with `image_tag` set to that tag -- no rebuild.
The workflow checks out that exact commit and re-syncs `docker-compose.prod.yml` and
`clickhouse/init/` from it to the deploy host before pulling/starting anything, so the image and
its deployment configuration always come from the same commit, on a rollback exactly as on a
forward deploy (see decision 13's "Deployment configuration travels with the image, from the exact
same commit"). `infrastructure/.env.production` is intentionally excluded from this sync -- it is
host-local and operator-owned, never versioned, and is therefore never rolled back either; an
operator who changed it between commit A and commit B must reconcile it themselves before or after
dispatching a rollback to A. The schema-compatibility caveat in decision 13 ("Rollback assumes
schema forward/backward compatibility, not automated downgrade") still applies unchanged.

**Compose-native rollback (Phase 4B, unchanged), for anything predating a registry publish, or a
compose-file-level revert:** `git checkout <previous-tag-or-commit>` followed by `docker compose
-f infrastructure/docker-compose.prod.yml --env-file infrastructure/.env.production up -d --build`
rebuilds and restarts every application service from the prior commit's Dockerfiles and source.
Database rollback uses Alembic's existing, unmodified `downgrade` capability, invoked manually by
an operator -- this phase does not automate migration rollback, only ensures forward migration
runs correctly as a container. No blue-green deployment, canary, or automated rollback tooling is
introduced.

## Known limitations

- **~~No I/O timeout on PostgreSQL calls~~ -- resolved, Phase 4D, both services.**
  `services/worker/worker/postgres/client.py`'s `get_connection()` sets both libpq's
  `connect_timeout` and PostgreSQL's own server-side `statement_timeout` from the worker's
  `database_timeout_seconds` setting (mirroring `worker/clickhouse/client.py`'s pre-existing
  `connect_timeout`/`send_receive_timeout` pattern) -- a stuck PostgreSQL call can no longer block
  a worker thread indefinitely. `apps/api/app/db/session.py`'s SQLAlchemy `engine` closes the
  identical gap on the API side, via the same `connect_timeout`/`statement_timeout` pair passed as
  `connect_args` (SQLAlchemy's documented mechanism for forwarding driver-specific connection
  parameters to `psycopg`), bounded by its own `database_timeout_seconds` setting -- a stuck query
  can no longer occupy an API request thread indefinitely either, and `pool_pre_ping`'s own
  liveness `SELECT` is now implicitly bounded by the same setting as a side effect. One residual,
  explicitly accepted gap remains on both sides: a network partition occurring *after* a
  connection is established, where the server's own cancellation response never arrives, could in
  principle still exceed this bound -- see each module's own docstring.
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
  Docker's `HEALTHCHECK` only *labels* a container unhealthy; it never restarts one, and
  `restart: unless-stopped` reacts only to process exit. So `worker/heartbeat.py`'s
  `HeartbeatWatchdog` (a daemon thread started by `WorkerRuntime.run`/`Poller.run`, enabled by
  `worker/__main__.py`/`poller_main.py`) force-exits the process (`os._exit(70)`) when the heartbeat
  has been stale for 2x `heartbeat_stale_seconds` (360s by default -- well after the healthcheck
  reports unhealthy, so a merely busy iteration is never killed), and the restart policy then
  recovers it. It reads the in-memory timestamp `touch_heartbeat()` refreshes, so an unwritable
  `/tmp` can never cause a kill. Once a stop has been requested (SIGTERM or the orphaned-evaluator
  self-retirement) staleness is expected and ignored; the watchdog then fires only if the process is
  still alive that same interval after the stop request, i.e. the shutdown itself hung. The deploy
  workflow's health check also waits for the `worker` and `poller` containers to report `healthy`.
  The poller refreshes its heartbeat after every job-creation call (not only once per tick), so a
  slow-but-working batch of hundreds of sequential calls is never mistaken for a stuck loop.
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
- **Container logs are size-bounded.** Docker's default `json-file` driver never rotates, so every
  service in `docker-compose.prod.yml` shares one `x-logging` anchor: 5 files x 10 MB per container
  (~350 MB for the stack). Only Docker's log files are affected -- not application logging or any
  volume. Raise the limits there for a longer local history; ship logs off-host for anything
  long-term.
- **No TLS termination or reverse proxy.** `docker-compose.prod.yml` exposes `api` and `dashboard`
  as plain HTTP on the host. TLS termination is assumed to be handled by infrastructure the
  operator already has in front of this stack (a load balancer, an existing reverse proxy) --
  introducing one here would be new infrastructure beyond Phase 4B's approved scope.
- **~~No CI/CD, registry, or automated image-publishing pipeline~~ -- resolved, Phase 4D (F7).**
  See decision 13.
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
- Future work reintroducing rate limiting, TLS termination, or backup/restore should treat this
  ADR's Compose topology as the base to extend, not redesign, unless a genuine architectural reason
  emerges.
- `ci.yml`'s `docker-build` matrix and `cd.yml`'s `publish` matrix duplicate the same three
  (context, dockerfile, build-contexts) triples by necessity -- one validates a build with no push,
  the other pushes. Adding a fourth service/image later means updating both, plus
  `docker-compose.prod.yml`'s corresponding `image:`/`build:` pair and
  `infrastructure/.env.production.example`.
