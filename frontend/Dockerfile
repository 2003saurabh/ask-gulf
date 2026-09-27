# syntax=docker/dockerfile:1
###############################################################################
# Ask Gulf frontend — Next.js 14 (App Router) production image.
#
# Multi-stage build using Next's "standalone" output so the runtime image ships
# only server.js + the minimal node_modules it needs.
#
# NOTE: NEXT_PUBLIC_* values are inlined into the client bundle at BUILD time,
# so the browser-visible backend WebSocket URL must be provided as a build ARG
# (NEXT_PUBLIC_WS_BASE_URL), not just a runtime env var. It points at the
# host-published backend port because the browser — not this container — opens
# the WebSocket to /ws/{session_id}.
###############################################################################

FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Backend WebSocket base as seen from the user's browser (default: localhost).
ARG NEXT_PUBLIC_WS_BASE_URL=ws://localhost:8080
ENV NEXT_PUBLIC_WS_BASE_URL=${NEXT_PUBLIC_WS_BASE_URL}
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
# Standalone server bundle + static assets.
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
