FROM nginx:1.27-alpine

# htpasswd (basic auth, docs/11 §5) + curl (compose healthcheck)
RUN apk add --no-cache apache2-utils curl

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY site/ /usr/share/nginx/html/
COPY docker-entrypoint.d/ /docker-entrypoint.d/
RUN chmod +x /docker-entrypoint.d/*.sh
