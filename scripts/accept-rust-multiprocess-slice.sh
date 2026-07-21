#!/usr/bin/env bash
set -u

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
MATURIN="$ROOT/.venv/bin/maturin"

if [[ ! -x "$PYTHON" || ! -x "$MATURIN" ]]; then
    echo "Rust-parent multiprocess acceptance: CPython 3.14 and maturin are required" >&2
    exit 1
fi

echo "Rust-parent multiprocess acceptance: building Python-outer Rust fixture"
if ! timeout --foreground 120s "$MATURIN" develop \
    --locked \
    --manifest-path "$ROOT/research/fixtures/python_outer/Cargo.toml"; then
    echo "Rust-parent multiprocess acceptance: fixture build failed" >&2
    exit 1
fi

echo "Rust-parent multiprocess acceptance: building Rust parent fixture"
if ! timeout --foreground 180s env \
    PYO3_PYTHON="$PYTHON" \
    cargo build --locked \
    --manifest-path "$ROOT/research/fixtures/rust_outer/Cargo.toml" \
    --bin rust-outer-python-processes; then
    echo "Rust-parent multiprocess acceptance: fixture build failed" >&2
    exit 1
fi

echo "Rust-parent multiprocess acceptance: coordinating two Python/Rust children"
exec timeout --foreground 240s "$PYTHON" -m tests.acceptance.run_rust_multiprocess_acceptance
