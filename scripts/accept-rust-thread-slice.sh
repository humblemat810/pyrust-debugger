#!/usr/bin/env bash
set -u

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "Rust-threaded acceptance: missing CPython 3.14 at $PYTHON" >&2
    exit 1
fi

echo "Rust-threaded acceptance: building Rust worker fixture"
if ! timeout --foreground 180s env \
    PYO3_PYTHON="$PYTHON" \
    cargo build --locked \
        --manifest-path "$ROOT/research/fixtures/rust_outer/Cargo.toml" \
        --bin rust-outer-python-threads; then
    echo "Rust-threaded acceptance: fixture build failed" >&2
    exit 1
fi

echo "Rust-threaded acceptance: running two-worker CodeLLDB checks"
exec timeout --foreground 180s "$PYTHON" -m tests.acceptance.run_rust_thread_acceptance
