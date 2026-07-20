#!/usr/bin/env bash
set -u

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
MATURIN="$ROOT/.venv/bin/maturin"

if [[ ! -x "$PYTHON" || ! -x "$MATURIN" ]]; then
    echo "process/thread acceptance: CPython 3.14 and maturin are required" >&2
    exit 1
fi

echo "process/thread acceptance: building Python-outer Rust extension"
if ! timeout --foreground 120s "$MATURIN" develop \
    --locked \
    --manifest-path "$ROOT/research/fixtures/python_outer/Cargo.toml"; then
    echo "process/thread acceptance: Python-outer fixture build failed" >&2
    exit 1
fi

echo "process/thread acceptance: building Rust process/thread parent"
if ! timeout --foreground 180s cargo build --locked \
    --manifest-path "$ROOT/research/fixtures/rust_outer/Cargo.toml" \
    --bin rust-outer-python-process-threads; then
    echo "process/thread acceptance: Rust fixture build failed" >&2
    exit 1
fi

echo "process/thread acceptance: proving process and native-thread ownership"
if ! timeout --foreground 240s "$PYTHON" \
    -m tests.acceptance.run_process_thread_mode_acceptance; then
    echo "process/thread acceptance: process/thread proof failed" >&2
    exit 1
fi

echo "process/thread acceptance: running Python async regression"
if ! "$ROOT/scripts/accept-async-slice.sh"; then
    echo "process/thread acceptance: Python async regression failed" >&2
    exit 1
fi

echo "process/thread acceptance: running Rust async regression"
if ! "$ROOT/scripts/accept-rust-async-slice.sh"; then
    echo "process/thread acceptance: Rust async regression failed" >&2
    exit 1
fi

echo "AC-PTM-07 PASS (async acceptance and nonnested process/thread model)"
