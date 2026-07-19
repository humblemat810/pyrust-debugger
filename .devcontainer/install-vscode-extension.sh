#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_VSIX="$ROOT/vscode-extension/pyrust-debugger.vsix"
IMAGE_VSIX="/opt/pyrust-extension/pyrust-debugger.vsix"
EXTENSION="pyrust.pyrust-debugger@0.0.1"
STATE_DIR="$HOME/.pyrust-debugger"
STATE_FILE="$STATE_DIR/vsix.sha256"

if ! command -v code >/dev/null 2>&1; then
    echo "PyRust extension install deferred until VS Code attaches"
    exit 0
fi

if [[ -f "$WORKSPACE_VSIX" ]]; then
    VSIX="$WORKSPACE_VSIX"
elif [[ -f "$IMAGE_VSIX" ]]; then
    VSIX="$IMAGE_VSIX"
else
    echo "PyRust extension install: packaged VSIX is missing" >&2
    exit 1
fi

is_installed() {
    code --list-extensions --show-versions \
        | tr -d '\r' \
        | grep -Fqx "$EXTENSION"
}

mkdir -p "$STATE_DIR"
vsix_sha="$(sha256sum "$VSIX" | cut -d' ' -f1)"

if is_installed \
    && [[ -f "$STATE_FILE" ]] \
    && [[ "$(cat "$STATE_FILE")" == "$vsix_sha" ]]
then
    echo "PyRust extension already installed from the current VSIX"
    exit 0
fi

code --install-extension "$VSIX" --force

for _ in {1..20}; do
    if is_installed; then
        printf '%s\n' "$vsix_sha" >"$STATE_FILE"
        echo "PyRust extension installed through the VS Code extension manager"
        exit 0
    fi
    sleep 0.25
done

echo "PyRust extension install completed but registration was not observed" >&2
exit 1
