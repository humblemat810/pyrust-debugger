#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

required_files=(
    README.md
    docs/submission/checklist.md
    docs/submission/demo-runbook.md
    docs/submission/form-copy.md
    docs/submission/verification-2026-07-21.md
    scripts/verify-submission.sh
)

for path in "${required_files[@]}"; do
    if [[ ! -f "$path" ]]; then
        echo "submission status: required artifact is missing: $path" >&2
        exit 1
    fi
done

NODE_BIN="/opt/node/bin/node"
if [[ ! -x "$NODE_BIN" ]]; then
    NODE_BIN="$(command -v node)"
fi
extension_version="$(
    env -u NODE_OPTIONS "$NODE_BIN" -p \
        'require("./vscode-extension/package.json").version'
)"

printf 'submission commit: %s\n' "$(git rev-parse HEAD)"
printf 'submission branch: %s\n' "$(git branch --show-current)"
printf 'PyRust VSIX version: %s\n' "$extension_version"

if [[ -n "$(git status --short)" ]]; then
    echo "SUBMISSION-STATUS DIRTY"
    echo "Commit or explicitly record the pending files before recording/submitting."
    git status --short
    exit 1
fi

echo "SUBMISSION-STATUS READY"
