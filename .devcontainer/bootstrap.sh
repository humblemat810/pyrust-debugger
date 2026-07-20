#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

uv sync --frozen --python 3.14.6
npm ci --prefix "$ROOT/vscode-extension"
npm run --prefix "$ROOT/vscode-extension" compile
npm run --prefix "$ROOT/vscode-extension" package

test -x "$PYRUST_CODELLDB"
test -f "$PYRUST_LIBLLDB"
test -x "$ROOT/.venv/bin/python"

# This installs immediately when the VS Code CLI is already available.
# postAttachCommand repeats it when the create lifecycle runs before attach.
bash "$ROOT/.devcontainer/install-vscode-extension.sh"

echo "PyRust Dev Container bootstrap complete"
