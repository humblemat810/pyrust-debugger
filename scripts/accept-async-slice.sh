#!/usr/bin/env bash
set -u

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
MATURIN="$ROOT/.venv/bin/maturin"

if [[ ! -x "$PYTHON" || ! -x "$MATURIN" ]]; then
    echo "async acceptance: CPython 3.14 and maturin are required" >&2
    exit 1
fi

echo "async acceptance: building Python-outer Rust fixture"
if ! timeout --foreground 120s "$MATURIN" develop \
    --locked \
    --manifest-path "$ROOT/research/fixtures/python_outer/Cargo.toml"; then
    echo "async acceptance: fixture build failed" >&2
    exit 1
fi

echo "async acceptance: running two-task CodeLLDB checks"
exec timeout --foreground 180s "$PYTHON" -m tests.acceptance.run_async_acceptance
