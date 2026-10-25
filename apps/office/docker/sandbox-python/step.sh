#!/bin/sh
set -eu

STEP="$1"
shift

if [ "$STEP" = install ]; then
  mkdir -p /deps/project
  cp /src/pyproject.toml /src/uv.lock /deps/project/
  uv sync --locked --no-install-project --project /deps/project
  sha256sum /deps/project/uv.lock | cut -d ' ' -f 1 > /deps/.office-complete
  exit 0
fi

if [ "$STEP" = lock ]; then
  mkdir -p /work/project
  cp /src/pyproject.toml /src/uv.lock /work/project/
  cd /work/project
  uv lock
  cp uv.lock /out/uv.lock
  exit 0
fi

if [ "$STEP" = test ]; then
  cd /src/api
  exec /deps/venv/bin/python -m pytest -p no:cacheprovider "$@"
fi

echo "unknown step: $STEP" >&2
exit 2
