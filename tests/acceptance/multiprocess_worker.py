"""One registered Python process that enters the Python-outer Rust fixture."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sys
import time


FIXTURE = Path(__file__).resolve().parents[2] / "research" / "fixtures" / "python_outer"
sys.path.insert(0, str(FIXTURE))


def python_worker(label: str, value: int) -> None:
    import pyrust_native

    registry = Path(os.environ["PYRUST_CHILD_REGISTRY"])
    registry.mkdir(parents=True, exist_ok=True)
    # The child adapter is neither this process nor its parent. This fixture
    # opts in to ptrace under Linux's restricted ptrace policy.
    ctypes.CDLL(None, use_errno=True).prctl(0x59616D61, -1, 0, 0, 0)
    process_id = os.getpid()
    (registry / f"child-{process_id}.json").write_text(
        json.dumps(
            {
                "pid": process_id,
                "parentPid": os.getppid(),
                "label": label,
                "value": value,
            }
        ),
        encoding="utf-8",
    )
    (registry / f"ready-{process_id}").touch()
    attached = registry / f"attached-{process_id}"
    release = registry / "release"
    deadline = time.monotonic() + 20
    while not attached.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("PyRust coordinator did not attach to child")
        time.sleep(0.02)
    while not release.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("PyRust coordinator did not release child")
        time.sleep(0.02)

    worker_label = label
    worker_value = value
    result = pyrust_native.rust_outer(worker_value)
    print(f"child {worker_label}: {result}", flush=True)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: multiprocess_worker.py LABEL VALUE")
    python_worker(sys.argv[1], int(sys.argv[2]))


if __name__ == "__main__":
    main()
