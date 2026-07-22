"""Enter PyO3 through names unrelated to the original research fixture."""

from __future__ import annotations

import sys
from pathlib import Path


FIXTURE = Path(__file__).resolve().parents[2] / "research" / "fixtures" / "python_outer"
sys.path.insert(0, str(FIXTURE))

import pyrust_native  # noqa: E402


def handle_event(value: int) -> int:
    request_label = "arbitrary-boundary"
    request_value = value
    return pyrust_native.dispatch_payload(request_value)


if __name__ == "__main__":
    print(handle_event(35), flush=True)
