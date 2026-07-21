#!/usr/bin/env bash
set -u

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
MATURIN="$ROOT/.venv/bin/maturin"

if [[ ! -x "$PYTHON" || ! -x "$MATURIN" ]]; then
    echo "Python/Rust thread acceptance: CPython 3.14 and maturin are required" >&2
    exit 1
fi

echo "Python/Rust thread acceptance: building the Python-outer Rust fixture"
if ! timeout --foreground 120s "$MATURIN" develop \
    --locked \
    --manifest-path "$ROOT/research/fixtures/python_outer/Cargo.toml"; then
    echo "Python/Rust thread acceptance: fixture build failed" >&2
    exit 1
fi

echo "Python/Rust thread acceptance: proving Python-to-Rust worker fan-out"
exec timeout --foreground 240s "$PYTHON" \
    -m tests.acceptance.run_python_rust_thread_acceptance
