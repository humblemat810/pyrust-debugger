"""Python workers enter Rust, which creates independent Rust worker threads."""

from __future__ import annotations

import sys
from pathlib import Path
from threading import Barrier, Event, Thread


FIXTURE = Path(__file__).resolve().parents[2] / "research" / "fixtures" / "python_outer"
sys.path.insert(0, str(FIXTURE))

import pyrust_native  # noqa: E402


READY = Barrier(3)
RESULTS: dict[str, int] = {}
FIRST_WORKER_FINISHED = Event()


def python_worker(label: str, value: int, waits_for_first: bool) -> None:
    python_label = label
    python_value = value
    READY.wait()
    if waits_for_first:
        FIRST_WORKER_FINISHED.wait()
    RESULTS[python_label] = pyrust_native.rust_outer_with_rust_threads(python_value)
    if not waits_for_first:
        FIRST_WORKER_FINISHED.set()


def main() -> None:
    workers = [
        Thread(
            target=python_worker,
            args=("python-worker-A", 20, False),
            name="python-worker-A",
        ),
        Thread(
            target=python_worker,
            args=("python-worker-B", 40, True),
            name="python-worker-B",
        ),
    ]
    for worker in workers:
        worker.start()
    READY.wait()
    for worker in workers:
        worker.join()
    print(f"Python/Rust thread results: {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
