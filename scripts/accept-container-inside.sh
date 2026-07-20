#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
EXTENSION="$ROOT/vscode-extension"

cd "$ROOT"

check_versions() {
    test -f "$ROOT/uv.lock"
    test -f "$EXTENSION/package-lock.json"
    test -f "$ROOT/research/fixtures/python_outer/Cargo.lock"
    test -f "$ROOT/research/fixtures/rust_outer/Cargo.lock"

    uv sync --frozen --python 3.14.6
    test "$("$PYTHON" --version)" = "Python 3.14.6"
    test "$(rustc --version | cut -d' ' -f1-2)" = "rustc 1.97.1"
    test "$(node --version)" = "v24.18.0"
    test "$(uname -m)" = "x86_64"

    node -e '
const manifest = require(
  "/opt/vscode-extensions/vadimcn.vscode-lldb-1.12.2/package.json",
);
if (manifest.version !== "1.12.2") {
  throw new Error(`unexpected CodeLLDB version: ${manifest.version}`);
}
'
}

check_extension() {
    npm ci --prefix "$EXTENSION"
    npm run --prefix "$EXTENSION" compile
    npm run --prefix "$EXTENSION" package
    npm audit --prefix "$EXTENSION" --audit-level=high

    node -e '
const manifest = require("./vscode-extension/package.json");
const debuggerContribution = manifest.contributes?.debuggers?.find(
  (candidate) => candidate.type === "pyrust",
);
if (!debuggerContribution) {
  throw new Error("pyrust debugger contribution is missing");
}
if (!manifest.activationEvents?.includes("onDebug")) {
  throw new Error("onDebug activation event is missing");
}
if (manifest.main !== "./out/extension.js") {
  throw new Error(`unexpected extension entry point: ${manifest.main}`);
}
'

    test -f "$EXTENSION/pyrust-debugger.vsix"
    test -f /opt/pyrust-extension/pyrust-debugger.vsix
    test -x "$ROOT/.devcontainer/install-vscode-extension.sh"
    node -e '
const fs = require("node:fs");
const container = JSON.parse(
  fs.readFileSync(".devcontainer/devcontainer.json", "utf8"),
);
if (
  container.postAttachCommand !==
  "bash .devcontainer/install-vscode-extension.sh"
) {
  throw new Error("postAttachCommand does not install the PyRust extension");
}
const tasks = JSON.parse(fs.readFileSync(".vscode/tasks.json", "utf8"));
for (const label of [
  "PyRust: Build Python Outer",
  "PyRust: Build Rust Outer",
]) {
  const task = tasks.tasks.find((candidate) => candidate.label === label);
  if (task?.dependsOn !== "PyRust: Ensure Debugger Extension") {
    throw new Error(`${label} does not ensure extension installation`);
  }
}
'
}

check_explicit_codelldb() {
    "$PYTHON" -m unittest prototype.adapter.tests.test_main
    "$PYTHON" - <<'PY'
import os

from prototype.adapter.__main__ import _default_codelldb_command

expected = [
    os.environ["PYRUST_CODELLDB"],
    "--liblldb",
    os.environ["PYRUST_LIBLLDB"],
]
actual = _default_codelldb_command()
if actual != expected:
    raise SystemExit(f"unexpected CodeLLDB command: {actual!r}")
PY

    timeout --foreground 15s \
        "$PYTHON" "$ROOT/prototype/adapter/__main__.py" \
        --codelldb "$PYRUST_CODELLDB" \
        --liblldb "$PYRUST_LIBLLDB" \
        </dev/null
}

check_first_slice() {
    "$ROOT/scripts/accept-first-slice.sh"
}

check_reverse_slice() {
    "$ROOT/scripts/accept-reverse-slice.sh"
}

check_extension_host() {
    timeout --foreground 720s \
        xvfb-run -a npm test --prefix "$EXTENSION"
}

check_clean_processes() {
    local match
    for match in \
        '[p]rototype/adapter/__main__.py' \
        '[/]adapter/codelldb' \
        '[p]ython_outer/app.py' \
        '[r]ust-outer-python-inner'
    do
        if pgrep -af "$match"; then
            echo "container acceptance: lingering debugger process matched $match" >&2
            return 1
        fi
    done
}

check_repeat() {
    check_versions
    check_extension
    check_explicit_codelldb
    check_first_slice
    check_reverse_slice
    check_extension_host
    check_clean_processes
}

case "${1:-}" in
    versions)
        check_versions
        ;;
    extension)
        check_extension
        ;;
    explicit-codelldb)
        check_explicit_codelldb
        ;;
    first-slice)
        check_first_slice
        ;;
    reverse-slice)
        check_reverse_slice
        ;;
    extension-host)
        check_extension_host
        ;;
    clean-processes)
        check_clean_processes
        ;;
    repeat)
        check_repeat
        ;;
    *)
        echo \
            "usage: $0 {versions|extension|explicit-codelldb|first-slice|reverse-slice|extension-host|clean-processes|repeat}" \
            >&2
        exit 2
        ;;
esac
