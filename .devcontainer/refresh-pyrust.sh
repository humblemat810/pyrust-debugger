#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Keep the mounted venv aligned with uv.lock after a workspace update.
uv sync --frozen --python 3.14.6

# The local VSIX contains both extension code and debugger configuration
# metadata, so package it before asking VS Code to install the current build.
env -u NODE_OPTIONS npm run --prefix "$ROOT/vscode-extension" compile
env -u NODE_OPTIONS npm run --prefix "$ROOT/vscode-extension" package
bash "$ROOT/.devcontainer/install-vscode-extension.sh"
