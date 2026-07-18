#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
NODE_VERSION="24.18.0"
NODE_SHA256="55aa7153f9d88f28d765fcdad5ae6945b5c0f98a36881703817e4c450fa76742"
NODE_HOME="$ROOT/.cache/tools/node-v$NODE_VERSION-linux-x64"
ARCHIVE="$ROOT/.cache/downloads/node-v$NODE_VERSION-linux-x64.tar.xz"
CLI_ROOT="$ROOT/tools/devcontainer-cli"

if [[ ! -x "$NODE_HOME/bin/node" ]]; then
    mkdir -p "$(dirname -- "$ARCHIVE")" "$(dirname -- "$NODE_HOME")"
    if [[ ! -f "$ARCHIVE" ]]; then
        curl -fL \
            "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz" \
            -o "$ARCHIVE"
    fi
    echo "$NODE_SHA256  $ARCHIVE" | sha256sum -c - >&2
    staging="$NODE_HOME.staging.$$"
    rm -rf "$staging"
    mkdir -p "$staging"
    tar -xJf "$ARCHIVE" --strip-components=1 -C "$staging"
    rm -rf "$NODE_HOME"
    mv "$staging" "$NODE_HOME"
fi

if [[ "$("$NODE_HOME/bin/node" --version)" != "v$NODE_VERSION" ]]; then
    echo "container acceptance: pinned Node bootstrap failed" >&2
    exit 1
fi

export PATH="$NODE_HOME/bin:$PATH"
npm ci --prefix "$CLI_ROOT" >&2

CLI="$CLI_ROOT/node_modules/.bin/devcontainer"
if [[ ! -x "$CLI" ]]; then
    echo "container acceptance: Dev Container CLI was not installed" >&2
    exit 1
fi

printf '%s\n' "$CLI"
