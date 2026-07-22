#!/usr/bin/env bash
set -u

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
MATURIN="$ROOT/.venv/bin/maturin"

if [[ ! -x "$PYTHON" || ! -x "$MATURIN" ]]; then
    echo "debugpy acceptance: CPython 3.14 and maturin are required" >&2
    exit 1
fi

echo "debugpy acceptance: building Python-outer Rust extension"
if ! timeout --foreground 120s "$MATURIN" develop \
    --locked \
    --manifest-path "$ROOT/research/fixtures/python_outer/Cargo.toml"; then
    echo "debugpy acceptance: Python-outer fixture build failed" >&2
    exit 1
fi

echo "debugpy acceptance: building subinterpreter-safe Rust extension"
if ! timeout --foreground 120s env \
    PYO3_PYTHON="$PYTHON" \
    cargo build --locked \
        --manifest-path \
        "$ROOT/research/fixtures/subinterpreter_outer/Cargo.toml"; then
    echo "debugpy acceptance: subinterpreter fixture build failed" >&2
    exit 1
fi

echo "debugpy acceptance: building Rust-outer embedded-Python fixture"
if ! timeout --foreground 180s env \
    PYO3_PYTHON="$PYTHON" \
    cargo build --locked \
        --manifest-path "$ROOT/research/fixtures/rust_outer/Cargo.toml" \
        --bin rust-outer-python-inner \
        --bin rust-outer-python-threads \
        --bin rust-outer-python-async; then
    echo "debugpy acceptance: Rust-outer fixture build failed" >&2
    exit 1
fi

echo "debugpy acceptance: running Python, thread, process, and Rust handoff checks"
exec timeout --foreground 300s "$PYTHON" -m tests.acceptance.run_debugpy_acceptance
