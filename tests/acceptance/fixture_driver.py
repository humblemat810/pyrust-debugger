"""Acceptance-only driver that exercises the existing fixture call path twice."""

from __future__ import annotations

import sys
from pathlib import Path


FIXTURE = Path(__file__).resolve().parents[2] / "research" / "fixtures" / "python_outer"
sys.path.insert(0, str(FIXTURE))

import app  # noqa: E402


def main() -> None:
    for index in (1, 2):
        result = app.python_outer()
        print(f"acceptance call {index}: {result}", flush=True)


if __name__ == "__main__":
    main()
