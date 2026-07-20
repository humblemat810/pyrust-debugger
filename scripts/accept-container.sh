#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER_WORKSPACE="/workspaces/pyrust-debugger"
NODE="$ROOT/.cache/tools/node-v24.18.0-linux-x64/bin/node"
CLI_JS="$ROOT/tools/devcontainer-cli/node_modules/@devcontainers/cli/devcontainer.js"
VOLUMES=(
    pyrust-debugger-venv
    pyrust-debugger-node-modules
    pyrust-debugger-cargo-target
)
CRITERIA=(
    AC-CV-01
    AC-CV-02
    AC-CV-03
    AC-CV-04
    AC-CV-05
    AC-CV-06
    AC-CV-07
    AC-CV-08
    AC-CV-09
    AC-CV-10
)

declare -A RESULTS=()
CONTAINER_ID=""
TMP="$(mktemp -d)"
BASELINE_STATUS="$TMP/git-status.before"
CURRENT_STATUS="$TMP/git-status.after"

cd "$ROOT"

snapshot_workspace() {
    local path mode digest
    while IFS= read -r -d '' path; do
        if [[ -L "$path" ]]; then
            mode="$(stat --format='%a' -- "$path")"
            digest="symlink:$(readlink -- "$path")"
        elif [[ -f "$path" ]]; then
            mode="$(stat --format='%a' -- "$path")"
            digest="$(sha256sum -- "$path" | cut -d' ' -f1)"
        else
            continue
        fi
        printf '%s\t%s\t%s\n' "$mode" "$digest" "$path"
    done < <(git ls-files -co --exclude-standard -z)
}

# Check source content rather than Git index state: acceptance must not rewrite
# files, but a user may stage their already-existing edits while it runs.
snapshot_workspace | LC_ALL=C sort >"$BASELINE_STATUS"

project_containers() {
    docker ps -aq \
        --filter "label=devcontainer.local_folder=$ROOT"
}

remove_project_containers() {
    local containers=()
    mapfile -t containers < <(project_containers)
    if ((${#containers[@]} > 0)); then
        docker rm -f "${containers[@]}" >/dev/null
    fi
    CONTAINER_ID=""
}

remove_project_volumes() {
    local volume
    for volume in "${VOLUMES[@]}"; do
        if docker volume inspect "$volume" >/dev/null 2>&1; then
            docker volume rm "$volume" >/dev/null
        fi
    done
}

cleanup() {
    remove_project_containers || true
    remove_project_volumes || true
}

finish() {
    local status=$?
    local criterion
    trap - EXIT

    cleanup
    snapshot_workspace | LC_ALL=C sort >"$CURRENT_STATUS"
    if ! cmp -s "$BASELINE_STATUS" "$CURRENT_STATUS"; then
        echo "container acceptance: workspace content changed during acceptance" >&2
        diff -u "$BASELINE_STATUS" "$CURRENT_STATUS" >&2 || true
        RESULTS[AC-CV-10]=FAIL
        status=1
    fi

    for criterion in "${CRITERIA[@]}"; do
        printf '%s %s\n' "$criterion" "${RESULTS[$criterion]:-FAIL}"
        if [[ "${RESULTS[$criterion]:-FAIL}" != PASS ]]; then
            status=1
        fi
    done

    rm -rf "$TMP"
    exit "$status"
}
trap finish EXIT

mark_pass() {
    RESULTS["$1"]=PASS
}

run_devcontainer() {
    "$NODE" "$CLI_JS" "$@"
}

find_running_container() {
    local containers=()
    mapfile -t containers < <(
        docker ps -q \
            --filter "label=devcontainer.local_folder=$ROOT"
    )
    if ((${#containers[@]} != 1)); then
        echo \
            "container acceptance: expected one running project container, found ${#containers[@]}" \
            >&2
        return 1
    fi
    CONTAINER_ID="${containers[0]}"
}

start_container() {
    local no_cache="$1"
    local arguments=(
        up
        --workspace-folder "$ROOT"
        --remove-existing-container
        --log-level info
    )
    if [[ "$no_cache" == true ]]; then
        arguments+=(--build-no-cache)
    fi
    run_devcontainer "${arguments[@]}"
    find_running_container
}

exec_inside() {
    run_devcontainer exec \
        --workspace-folder "$ROOT" \
        bash scripts/accept-container-inside.sh "$1"
}

check_storage_isolation() {
    local mounts
    mounts="$(docker inspect --format \
        '{{range .Mounts}}{{printf "%s %s\n" .Destination .Type}}{{end}}' \
        "$CONTAINER_ID")"
    grep -Fx "$CONTAINER_WORKSPACE/.venv volume" <<<"$mounts"
    grep -Fx "$CONTAINER_WORKSPACE/vscode-extension/node_modules volume" \
        <<<"$mounts"
    grep -Fx "/opt/pyrust-target volume" <<<"$mounts"
    docker exec "$CONTAINER_ID" \
        test -x "$CONTAINER_WORKSPACE/.venv/bin/python"
    docker exec "$CONTAINER_ID" test \
        "$(
            docker exec "$CONTAINER_ID" printenv CARGO_TARGET_DIR
        )" = /opt/pyrust-target
}

check_security() {
    local capability security_options mounts
    test "$(docker inspect --format '{{.HostConfig.Privileged}}' "$CONTAINER_ID")" = false
    test -z "$(docker inspect --format '{{.HostConfig.PidMode}}' "$CONTAINER_ID")"

    capability="$(docker inspect --format '{{json .HostConfig.CapAdd}}' "$CONTAINER_ID")"
    grep -Fq SYS_PTRACE <<<"$capability"

    security_options="$(
        docker inspect --format '{{json .HostConfig.SecurityOpt}}' "$CONTAINER_ID"
    )"
    grep -Fq seccomp=unconfined <<<"$security_options"

    mounts="$(docker inspect --format \
        '{{range .Mounts}}{{println .Destination}}{{end}}' \
        "$CONTAINER_ID")"
    if grep -Fxq /var/run/docker.sock <<<"$mounts"; then
        echo "container acceptance: Docker socket is mounted" >&2
        return 1
    fi

    docker exec "$CONTAINER_ID" capsh --print | grep -Fq cap_sys_ptrace
}

stop_container_normally() {
    docker stop --time 20 "$CONTAINER_ID" >/dev/null
    test "$(docker inspect --format '{{.State.Status}}' "$CONTAINER_ID")" = exited
    docker rm "$CONTAINER_ID" >/dev/null
    CONTAINER_ID=""
}

command -v docker >/dev/null
test "$(docker info --format '{{.Architecture}}')" = x86_64
"$ROOT/scripts/lib/ensure-devcontainer-cli.sh" >/dev/null
test -x "$NODE"
test -f "$CLI_JS"
test "$("$NODE" --version)" = v24.18.0

cleanup
start_container true
check_storage_isolation
mark_pass AC-CV-01

exec_inside versions
mark_pass AC-CV-02

check_security
mark_pass AC-CV-03

exec_inside extension
mark_pass AC-CV-04

exec_inside explicit-codelldb
mark_pass AC-CV-05

exec_inside first-slice
mark_pass AC-CV-06

exec_inside reverse-slice
mark_pass AC-CV-07

exec_inside extension-host
mark_pass AC-CV-08

exec_inside clean-processes
stop_container_normally
remove_project_volumes

start_container false
check_storage_isolation
check_security
exec_inside repeat
mark_pass AC-CV-09

exec_inside clean-processes
stop_container_normally
snapshot_workspace | LC_ALL=C sort >"$CURRENT_STATUS"
cmp -s "$BASELINE_STATUS" "$CURRENT_STATUS"
mark_pass AC-CV-10
