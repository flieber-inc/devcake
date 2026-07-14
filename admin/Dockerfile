# syntax=docker/dockerfile:1
# Admin SPA (Vite) + nginx reverse proxy. Multi-stage: Node build → alpine nginx.

ARG NODE_IMAGE=node:22-alpine
ARG NGINX_IMAGE=nginx:1.27-alpine

# ── SPA build ───────────────────────────────────────────────────────────────
FROM ${NODE_IMAGE} AS build

WORKDIR /spa

# Lockfile first → npm layer stays cached when only source changes.
COPY spa/package.json spa/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund

COPY spa/ ./
ENV NODE_ENV=production
RUN npm run build

# ── serve ───────────────────────────────────────────────────────────────────
FROM ${NGINX_IMAGE}

LABEL org.opencontainers.image.title="devcake-admin" \
      org.opencontainers.image.description="DevCake admin SPA (nginx)" \
      org.opencontainers.image.source="https://github.com/fidecastro/devcake"

# htpasswd (basic auth, docs/11 §5) + curl (compose healthcheck)
RUN apk add --no-cache \
        apache2-utils \
        curl

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /spa/dist/ /usr/share/nginx/html/
COPY docker-entrypoint.d/ /docker-entrypoint.d/
RUN chmod +x /docker-entrypoint.d/*.sh

EXPOSE 80
