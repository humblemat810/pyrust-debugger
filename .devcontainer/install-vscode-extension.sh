#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_VSIX="$ROOT/vscode-extension/pyrust-debugger.vsix"
IMAGE_VSIX="/opt/pyrust-extension/pyrust-debugger.vsix"
EXTENSION="pyrust.pyrust-debugger@0.0.1"
STATE_DIR="$HOME/.pyrust-debugger"
STATE_FILE="$STATE_DIR/vsix.sha256"

prepare_vscode_cli() {
    if command -v code >/dev/null 2>&1; then
        return 0
    fi

    local remote_cli
    remote_cli="$(
        find /vscode/vscode-server/bin \
            -type f \
            -path "*/bin/remote-cli/code" \
            -print \
            -quit \
            2>/dev/null || true
    )"
    if [[ -z "$remote_cli" ]]; then
        echo "PyRust extension install deferred until VS Code attaches"
        return 1
    fi

    if [[ -z "${VSCODE_IPC_HOOK_CLI:-}" ]]; then
        local ipc_socket=""
        for _ in {1..40}; do
            ipc_socket="$(
            find /tmp \
                -maxdepth 1 \
                -type s \
                -name "vscode-ipc-*.sock" \
                -printf "%T@ %p\n" \
                2>/dev/null \
                | sort -nr \
                | awk 'NR == 1 { print $2 }'
            )"
            if [[ -n "$ipc_socket" ]]; then
                break
            fi
            sleep 0.5
        done
        if [[ -z "$ipc_socket" ]]; then
            echo "PyRust extension install deferred until VS Code IPC is ready"
            return 1
        fi
        export VSCODE_IPC_HOOK_CLI="$ipc_socket"
    fi

    export PATH="$(dirname "$remote_cli"):$PATH"
    export TERM_PROGRAM="${TERM_PROGRAM:-vscode}"
    export VSCODE_INJECTION="${VSCODE_INJECTION:-1}"
    export VSCODE_STABLE="${VSCODE_STABLE:-1}"
}

prepare_vscode_cli || exit 0

if [[ -f "$WORKSPACE_VSIX" ]]; then
    VSIX="$WORKSPACE_VSIX"
elif [[ -f "$IMAGE_VSIX" ]]; then
    VSIX="$IMAGE_VSIX"
else
    echo "PyRust extension install: packaged VSIX is missing" >&2
    exit 1
fi

is_installed() {
    env -u NODE_OPTIONS code --list-extensions --show-versions \
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

env -u NODE_OPTIONS code --install-extension "$VSIX" --force

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
