#!/bin/sh
set -eu

SRC="${1:-/src}"

rm -rf /work/app /work/smoke
mkdir -p /work/app
tar -C "$SRC" --exclude=node_modules --exclude=dist -cf - . | tar -C /work/app -xf -
cp -r /smoke /work/smoke

cd /work/app
npm ci --no-audit --no-fund
npm run build

cd /work/smoke
playwright test --config playwright.config.ts
