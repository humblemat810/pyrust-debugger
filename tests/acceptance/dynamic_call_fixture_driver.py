"""Call the Rust extension through a dynamically selected Python alias."""

from __future__ import annotations

import sys
from pathlib import Path


FIXTURE = Path(__file__).resolve().parents[2] / "research" / "fixtures" / "python_outer"
sys.path.insert(0, str(FIXTURE))

import pyrust_native  # noqa: E402


def python_dynamic(value: int) -> int:
    label = "dynamic-python-to-rust"
    candidates = {"selected": pyrust_native.rust_outer}
    native_call = candidates["selected"]
    result = native_call(value)
    return result


def main() -> None:
    print(f"dynamic Python -> Rust result: {python_dynamic(20)}", flush=True)


if __name__ == "__main__":
    main()
