FROM python:3.12.15-slim

RUN pip install --no-cache-dir uv==0.12.23 \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid sandbox --create-home --shell /usr/sbin/nologin sandbox \
    && mkdir -p /work /deps \
    && chown sandbox:sandbox /work /deps

COPY step.sh /usr/local/bin/sandbox-python-step

ENV PYTHONDONTWRITEBYTECODE=1
ENV UV_PROJECT_ENVIRONMENT=/deps/venv
ENV UV_CACHE_DIR=/tmp/uv-cache
ENV UV_PYTHON_DOWNLOADS=never
ENV UV_LINK_MODE=copy

USER sandbox
WORKDIR /work
