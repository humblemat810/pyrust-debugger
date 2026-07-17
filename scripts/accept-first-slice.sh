#!/usr/bin/env bash
set -u

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
MATURIN="$ROOT/.venv/bin/maturin"

if [[ ! -x "$PYTHON" ]]; then
    echo "acceptance: missing CPython 3.14 at $PYTHON" >&2
    exit 1
fi
if [[ ! -x "$MATURIN" ]]; then
    echo "acceptance: missing maturin at $MATURIN" >&2
    exit 1
fi

echo "acceptance: building Python-outer fixture"
if ! timeout --foreground 120s "$MATURIN" develop \
    --manifest-path "$ROOT/research/fixtures/python_outer/Cargo.toml"; then
    echo "acceptance: fixture build failed" >&2
    exit 1
fi

echo "acceptance: running protocol and CodeLLDB black-box checks"
exec timeout --foreground 180s "$PYTHON" -m tests.acceptance.run_acceptance
