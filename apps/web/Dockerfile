FROM node:22-alpine AS build

WORKDIR /app
RUN corepack enable && corepack prepare pnpm@10.33.2 --activate

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY index.html tsconfig*.json vite.config.ts viteProxyTarget.ts ./
COPY public ./public
COPY src ./src

ARG VITE_NEXUS_AUTH_MODE=legacy-development
ENV VITE_NEXUS_AUTH_MODE=$VITE_NEXUS_AUTH_MODE
ENV VITE_NEXUS_CLIENT=http \
    VITE_NEXUS_BASE_URL=/
RUN pnpm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine

COPY --from=build --chown=nginx:nginx /app/dist /usr/share/nginx/html
COPY --chown=nginx:nginx nginx.conf /etc/nginx/conf.d/default.conf

USER nginx
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/health/live"]
