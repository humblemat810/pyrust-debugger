#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

uv sync --frozen --python 3.14.6
npm ci --prefix "$ROOT/vscode-extension"
npm run --prefix "$ROOT/vscode-extension" compile
npm run --prefix "$ROOT/vscode-extension" package

SERVER_EXTENSION="/root/.vscode-server/extensions/pyrust.pyrust-debugger-0.0.1"
mkdir -p "$SERVER_EXTENSION/out"
cp "$ROOT/vscode-extension/package.json" "$SERVER_EXTENSION/package.json"
cp "$ROOT/vscode-extension/README.md" "$SERVER_EXTENSION/README.md"
cp "$ROOT/vscode-extension/LICENSE" "$SERVER_EXTENSION/LICENSE"
cp -a "$ROOT/vscode-extension/out/." "$SERVER_EXTENSION/out/"

test -x "$PYRUST_CODELLDB"
test -f "$PYRUST_LIBLLDB"
test -x "$ROOT/.venv/bin/python"

echo "PyRust Dev Container bootstrap complete"
