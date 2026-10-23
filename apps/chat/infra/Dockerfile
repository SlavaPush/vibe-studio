FROM node:24.22.1-slim AS web
WORKDIR /src
COPY contracts/api.md contracts/api.md
COPY web/package.json web/package-lock.json web/
RUN cd web && npm ci
COPY web/ web/
RUN cd web && npm run build

FROM python:3.12.15-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY api/pyproject.toml api/uv.lock ./
RUN --mount=from=ghcr.io/astral-sh/uv:0.12.23,source=/uv,target=/bin/uv \
    uv sync --locked --no-dev --no-install-project --no-cache
COPY api/app/ app/
COPY --from=web /src/web/dist /srv/web
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
ENV PATH=/opt/venv/bin:$PATH \
    WEB_DIST_DIR=/srv/web
USER 10001
EXPOSE 10000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-10000}\" --no-proxy-headers --timeout-graceful-shutdown 3"]
