#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_VSIX="$ROOT/vscode-extension/pyrust-debugger.vsix"
IMAGE_VSIX="/opt/pyrust-extension/pyrust-debugger.vsix"
STATE_DIR="$HOME/.pyrust-debugger"
STATE_FILE="$STATE_DIR/vsix.sha256"

prepare_vscode_cli() {
    if ! command -v code >/dev/null 2>&1; then
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
        export PATH="$(dirname "$remote_cli"):$PATH"
    fi

    export TERM_PROGRAM="${TERM_PROGRAM:-vscode}"
    export VSCODE_INJECTION="${VSCODE_INJECTION:-1}"
    export VSCODE_STABLE="${VSCODE_STABLE:-1}"

    if vscode_cli_responds; then
        return 0
    fi

    # postAttachCommand can inherit a dead IPC socket from a previous Remote
    # window. Probe every recent socket rather than retrying that stale path.
    local candidate=""
    local inherited_socket="${VSCODE_IPC_HOOK_CLI:-}"
    for _ in {1..40}; do
        while IFS= read -r candidate; do
            [[ -n "$candidate" ]] || continue
            export VSCODE_IPC_HOOK_CLI="$candidate"
            if vscode_cli_responds; then
                return 0
            fi
        done < <(
            {
                [[ -n "$inherited_socket" ]] && printf '%s\n' "$inherited_socket"
                find /tmp \
                    -maxdepth 1 \
                    -type s \
                    -name "vscode-ipc-*.sock" \
                    -printf "%T@ %p\n" \
                    2>/dev/null \
                    | sort -nr \
                    | awk '{ print $2 }'
            } | awk '!seen[$0]++'
        )
        sleep 0.5
    done

    unset VSCODE_IPC_HOOK_CLI
    echo "PyRust extension install deferred until a live VS Code IPC socket is ready"
    return 1
}

vscode_cli_responds() {
    local output
    if ! output="$(
        env -u NODE_OPTIONS code --list-extensions --show-versions 2>&1
    )"; then
        return 1
    fi
    ! vscode_cli_output_is_error "$output"
}

vscode_cli_output_is_error() {
    local output="$1"
    grep -Eiq \
        'Unable to connect|ECONNREFUSED|Command is only available|Error in request' \
        <<<"$output"
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

extension_identity() {
    local node_bin="/opt/node/bin/node"
    if [[ ! -x "$node_bin" ]]; then
        node_bin="$(command -v node)"
    fi

    unzip -p "$VSIX" extension/package.json \
        | env -u NODE_OPTIONS "$node_bin" -e '
            let input = "";
            process.stdin.setEncoding("utf8");
            process.stdin.on("data", (chunk) => (input += chunk));
            process.stdin.on("end", () => {
                const manifest = JSON.parse(input);
                process.stdout.write(
                    `${manifest.publisher}.${manifest.name}@${manifest.version}`,
                );
            });
        '
}

EXTENSION="$(extension_identity)"

is_installed() {
    local extensions
    if ! extensions="$(
        env -u NODE_OPTIONS code --list-extensions --show-versions 2>&1
    )" || vscode_cli_output_is_error "$extensions"; then
        prepare_vscode_cli || return 1
        extensions="$(
            env -u NODE_OPTIONS code --list-extensions --show-versions 2>&1
        )" || return 1
        vscode_cli_output_is_error "$extensions" && return 1
    fi
    printf '%s\n' "$extensions" \
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

install_extension() {
    local output
    if ! output="$(
        env -u NODE_OPTIONS code --install-extension "$VSIX" --force 2>&1
    )" || vscode_cli_output_is_error "$output"; then
        [[ -z "$output" ]] || printf '%s\n' "$output" >&2
        return 1
    fi
    [[ -z "$output" ]] || printf '%s\n' "$output"
}

if ! install_extension; then
    echo "PyRust extension install retrying after VS Code IPC refresh"
    prepare_vscode_cli || exit 0
    install_extension
fi

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
