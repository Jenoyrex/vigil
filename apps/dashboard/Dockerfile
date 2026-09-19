# syntax=docker/dockerfile:1
#
# Production image for apps/dashboard (Next.js, output: "standalone").
#
# apps/dashboard is a pnpm WORKSPACE member (pnpm-workspace.yaml,
# pnpm-lock.yaml at the repository root) -- the lockfile's importer entries
# are keyed by workspace-relative path ("apps/dashboard"), so a
# `--frozen-lockfile` install must see that same relative layout, not just
# apps/dashboard's package.json in isolation. This build's primary context
# is apps/dashboard itself (so this directory's own .dockerignore applies
# normally); the two repo-root workspace files it needs
# (pnpm-lock.yaml, pnpm-workspace.yaml) are supplied as a separate, NAMED
# additional build context (BuildKit `--build-context`, Compose
# `build.additional_contexts`) called "workspace".
# packages/sdk-typescript is the other declared workspace member, but is an
# empty, untracked scaffold with no package.json and no lockfile entry
# (confirmed: `grep sdk-typescript pnpm-lock.yaml` matches nothing) -- it
# contributes nothing to this build and is deliberately not referenced.
#
# Because the pnpm workspace root (containing pnpm-lock.yaml) sits above
# apps/dashboard, Next's `output: "standalone"` file-tracing root is that
# workspace root, so `next build` emits a monorepo-shaped standalone
# tree (.next/standalone/apps/dashboard/server.js, hoisted node_modules)
# rather than a flat one -- accounted for explicitly below rather than
# changed via next.config.ts (Phase 4B's approved next.config.ts change is
# `output: "standalone"` only).
#
# Standalone build (from repo root):
#   docker build -f apps/dashboard/Dockerfile \
#     --build-context workspace=. \
#     apps/dashboard

FROM node:22-bookworm-slim AS base
RUN corepack enable && corepack prepare pnpm@11.9.0 --activate


FROM base AS deps

WORKDIR /build

COPY --from=workspace pnpm-lock.yaml pnpm-workspace.yaml ./
COPY package.json ./apps/dashboard/package.json

RUN pnpm install --frozen-lockfile --filter vigil-dashboard...


FROM base AS builder

WORKDIR /build

COPY --from=deps /build/node_modules ./node_modules
COPY --from=deps /build/apps/dashboard/node_modules ./apps/dashboard/node_modules
COPY --from=workspace pnpm-lock.yaml pnpm-workspace.yaml ./
COPY . ./apps/dashboard

WORKDIR /build/apps/dashboard
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm build


FROM node:22-bookworm-slim AS runtime

# uid/gid 1000 is already taken by the base image's built-in `node`
# user/group, so `vigil` here uses 1001 instead (apps/api and
# services/worker's base images have no such pre-existing user, so their
# Dockerfiles use 1000 without conflict).
RUN groupadd --system --gid 1001 vigil \
    && useradd --system --uid 1001 --gid vigil --no-create-home vigil

WORKDIR /app

# .next/standalone already contains only the traced production
# node_modules subset plus server.js -- Next.js deliberately excludes
# .next/static from it, so it's copied in separately. public/ is NOT
# copied: this app has no public/ directory (see next.config.ts's img-src
# comment -- no <img>/next/image usage, no static assets; the favicon is
# served from app/favicon.ico, the App Router convention, not public/). If
# apps/dashboard ever gains a public/ directory, add it back here.
COPY --from=builder --chown=vigil:vigil /build/apps/dashboard/.next/standalone/. ./
COPY --from=builder --chown=vigil:vigil /build/apps/dashboard/.next/static ./apps/dashboard/.next/static

ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    NEXT_TELEMETRY_DISABLED=1

USER vigil

EXPOSE 3000

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=5 \
    CMD node -e "require('http').get('http://127.0.0.1:3000/', r => process.exit(r.statusCode < 500 ? 0 : 1)).on('error', () => process.exit(1))"

CMD ["node", "apps/dashboard/server.js"]
