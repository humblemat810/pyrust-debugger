"""JSON command-line contract for the CPython 3.14 stack helper."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from typing import TextIO

from .unwinder import StackReadError, failure_payload, read_python_stacks, success_payload


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    """Write exactly one JSON result and return a process exit status."""
    if argv is None:
        argv = sys.argv[1:]
    if stdout is None:
        stdout = sys.stdout

    try:
        pid = _parse_pid(argv)
        payload = success_payload(read_python_stacks(pid))
    except StackReadError as error:
        payload = failure_payload(error)

    stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return 0 if payload["ok"] else 1


def _parse_pid(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        raise StackReadError("invalid_pid")
    try:
        pid = int(argv[0])
    except ValueError as error:
        raise StackReadError("invalid_pid") from error
    if pid <= 0:
        raise StackReadError("invalid_pid")
    return pid


if __name__ == "__main__":
    raise SystemExit(main())
