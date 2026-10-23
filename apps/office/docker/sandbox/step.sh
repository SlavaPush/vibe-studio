#!/bin/sh
set -eu

STEP="$1"
COMMAND="$2"

if [ "$STEP" = install ]; then
  cp /src/web/package.json /src/web/package-lock.json /deps/
  cd /deps
  sh -c "$COMMAND"
  sha256sum package-lock.json | cut -d ' ' -f 1 > /deps/.office-complete
  exit 0
fi

mkdir -p /work/app
tar -C /src --exclude=.git --exclude=node_modules --exclude=dist -cf - . | tar -C /work/app -xf -
mkdir /work/app/web/node_modules
for entry in /deps/node_modules/* /deps/node_modules/.[!.]*; do
  [ -e "$entry" ] || continue
  name="${entry##*/}"
  case "$name" in
    .tmp | .vite | .cache) ;;
    *) ln -s "$entry" "/work/app/web/node_modules/$name" ;;
  esac
done

cd /work/app/web
exec sh -c "$COMMAND"
