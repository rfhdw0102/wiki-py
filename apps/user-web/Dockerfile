# syntax=docker/dockerfile:1.7

FROM node:22-alpine AS base
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
ENV NEXT_TELEMETRY_DISABLED=1
RUN corepack enable
WORKDIR /app

FROM base AS dependencies
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/user-web/package.json apps/user-web/package.json
COPY apps/admin-web/package.json apps/admin-web/package.json
COPY packages/web-ui/package.json packages/web-ui/package.json
RUN --mount=type=cache,id=pnpm,target=/pnpm/store \
    pnpm install --frozen-lockfile --filter @wiki-agent/user-web...

FROM dependencies AS builder
COPY apps/user-web apps/user-web
COPY packages/web-ui packages/web-ui
RUN pnpm --filter @wiki-agent/user-web build

FROM node:22-alpine AS runner
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV HOSTNAME="0.0.0.0"
ENV PORT=3000
WORKDIR /app
RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs
COPY --from=builder --chown=nextjs:nodejs /app/apps/user-web/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/apps/user-web/.next/static ./apps/user-web/.next/static
USER nextjs
EXPOSE 3000
CMD ["node", "apps/user-web/server.js"]
