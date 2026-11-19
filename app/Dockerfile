# Main program (target "api") and task service (target "tasks") are built and
# released as separate images from the same code, so a task fix only replaces
# the task container while forwarding keeps running. Chromium is not in either
# image; it lives in the browser executor image (Dockerfile.browser).

FROM node:24-slim AS frontend
WORKDIR /build
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# Pinned official release and digest; only the forwarding image owns the kernel.
FROM python:3.12-slim AS proxy-kernel
ARG MIHOMO_VERSION=v1.19.34
ARG MIHOMO_SHA256=2763f5b414527f7baf6dc57e3f82f3ab561eedf34bfd7c2f2cde1f7e592efe06
RUN python -c "import urllib.request,hashlib,gzip,os; u='https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/mihomo-linux-amd64-compatible-${MIHOMO_VERSION}.gz'; b=urllib.request.urlopen(u,timeout=120).read(); assert hashlib.sha256(b).hexdigest()=='${MIHOMO_SHA256}'; open('/mihomo','wb').write(gzip.decompress(b)); os.chmod('/mihomo',0o755)"

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 unified
COPY backend/app/ ./backend/app/
RUN mkdir -p /app/.local && chown -R unified:unified /app
USER unified

FROM base AS tasks
ENV UNIFIED_ROLE=tasks
EXPOSE 4180
# Long jobs get time to reach a checkpoint; anything cut off is recovered by lease expiry.
CMD ["uvicorn", "app.main:create_app", "--factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "4180", "--no-access-log", "--timeout-graceful-shutdown", "60"]

FROM base AS api
ENV UNIFIED_ROLE=api
COPY --from=proxy-kernel /mihomo /usr/local/bin/mihomo
COPY --from=frontend --chown=unified:unified /build/dist ./web/dist
EXPOSE 4180
CMD ["uvicorn", "app.main:create_app", "--factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "4180", "--no-access-log", "--proxy-headers", "--timeout-graceful-shutdown", "30"]
