#!/usr/bin/env bash
set -u

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "acceptance: missing CPython 3.14 at $PYTHON" >&2
    exit 1
fi

echo "acceptance: building Rust-outer fixture"
if ! timeout --foreground 180s env \
    PYO3_PYTHON="$PYTHON" \
    cargo build --locked \
        --manifest-path "$ROOT/research/fixtures/rust_outer/Cargo.toml"; then
    echo "acceptance: Rust-outer fixture build failed" >&2
    exit 1
fi

echo "acceptance: running reverse contract and CodeLLDB black-box checks"
exec timeout --foreground 420s "$PYTHON" -m tests.acceptance.run_reverse_acceptance
