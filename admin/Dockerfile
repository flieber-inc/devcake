# ── build the SPA ──
FROM node:22-alpine AS build
WORKDIR /spa
COPY spa/package.json ./
RUN npm install
COPY spa/ ./
RUN npm run build

# ── serve ──
FROM nginx:1.27-alpine

# htpasswd (basic auth, docs/11 §5) + curl (compose healthcheck)
RUN apk add --no-cache apache2-utils curl

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /spa/dist/ /usr/share/nginx/html/
COPY docker-entrypoint.d/ /docker-entrypoint.d/
RUN chmod +x /docker-entrypoint.d/*.sh
