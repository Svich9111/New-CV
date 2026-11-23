# Caddy стенда со сборкой Mini App (задача 0.8). Контекст — корень репозитория
# (frontend/ и infra/deploy/Caddyfile); остальное отсекает caddy.Dockerfile.dockerignore.
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM caddy:2.10-alpine
COPY infra/deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=web /web/dist /srv/www
