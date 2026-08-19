# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
# (frontend digest-pinned — see images/Dockerfile line 1 for why + bump how)
# Admin SPA (Vite) + nginx reverse proxy. Multi-stage: Node build → alpine nginx.

# Digest-pinned. Bump: docker buildx imagetools inspect <image:tag>
ARG NODE_IMAGE=node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2
ARG NGINX_IMAGE=nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10

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

# image.source is omitted until a public product remote is published
# (pre-FOSS — do not hardcode a personal/private URL here).
LABEL org.opencontainers.image.title="devcake-admin" \
      org.opencontainers.image.description="DevCake admin SPA (nginx)"

# htpasswd (basic auth, docs/11 §5) + curl (compose healthcheck)
RUN apk add --no-cache \
        apache2-utils \
        curl

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /spa/dist/ /usr/share/nginx/html/
COPY docker-entrypoint.d/ /docker-entrypoint.d/
RUN chmod +x /docker-entrypoint.d/*.sh

EXPOSE 80
