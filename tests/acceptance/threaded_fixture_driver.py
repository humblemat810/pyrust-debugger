"""Two deterministic Python workers that enter the existing Rust fixture."""

from __future__ import annotations

import sys
from pathlib import Path
from threading import Barrier, Thread


FIXTURE = Path(__file__).resolve().parents[2] / "research" / "fixtures" / "python_outer"
sys.path.insert(0, str(FIXTURE))

import pyrust_native  # noqa: E402


READY = Barrier(3)
RESULTS: dict[str, int] = {}


def python_worker(label: str, value: int) -> None:
    worker_label = label
    worker_value = value
    READY.wait()
    result = pyrust_native.rust_outer(worker_value)
    RESULTS[worker_label] = result


def main() -> None:
    workers = [
        Thread(target=python_worker, args=("worker-A", 20), name="worker-A"),
        Thread(target=python_worker, args=("worker-B", 40), name="worker-B"),
    ]
    for worker in workers:
        worker.start()
    READY.wait()
    for worker in workers:
        worker.join()
    print(f"threaded results: {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
