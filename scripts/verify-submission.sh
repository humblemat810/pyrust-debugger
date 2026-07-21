#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
NODE_BIN="${PYRUST_NODE_BIN:-}"
if [[ -z "$NODE_BIN" ]]; then
    for candidate in \
        "$ROOT/.cache/tools/node-v24.18.0-linux-x64/bin" \
        "/opt/node/bin"
    do
        if [[ -x "$candidate/npm" ]]; then
            NODE_BIN="$candidate"
            break
        fi
    done
fi

if [[ ! -x "$PYTHON" ]]; then
    echo "submission verification: missing CPython 3.14 at $PYTHON" >&2
    exit 1
fi
if [[ ! -x "$NODE_BIN/npm" ]]; then
    echo "submission verification: missing pinned npm at $NODE_BIN/npm" >&2
    exit 1
fi

echo "submission verification: adapter unit coverage"
"$PYTHON" -m unittest prototype.adapter.tests.test_mixed_stack

echo "submission verification: Python to Rust mixed stack"
"$ROOT/scripts/accept-first-slice.sh"

echo "submission verification: Rust to Python callback stack"
"$ROOT/scripts/accept-reverse-slice.sh"

echo "submission verification: Python entry with Rust worker threads"
"$ROOT/scripts/accept-python-rust-threads.sh"

echo "submission verification: Python worker-thread regression"
"$ROOT/scripts/accept-thread-slice.sh"

echo "submission verification: Rust worker-thread regression"
"$ROOT/scripts/accept-rust-thread-slice.sh"

echo "submission verification: process and native-thread hierarchy"
"$ROOT/scripts/accept-process-thread-mode.sh"

echo "submission verification: VS Code extension compile and package"
env -u NODE_OPTIONS PATH="$NODE_BIN:$PATH" npm run --prefix vscode-extension compile
env -u NODE_OPTIONS PATH="$NODE_BIN:$PATH" npm run --prefix vscode-extension package

if [[ "${PYRUST_VERIFY_CONTAINER:-0}" == "1" ]]; then
    echo "submission verification: clean Dev Container acceptance"
    "$ROOT/scripts/accept-container.sh"
else
    echo "submission verification: clean Dev Container acceptance skipped"
    echo "set PYRUST_VERIFY_CONTAINER=1 to run the Docker-backed acceptance"
fi

echo "SUBMISSION-DEMO-GATE PASS"
