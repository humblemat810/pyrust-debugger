"""Spawn two independent Python processes that enter the Rust fixture."""

from __future__ import annotations

import multiprocessing as multiprocessing
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.acceptance.multiprocess_worker import python_worker as run_worker


def python_worker(label: str, value: int) -> None:
    run_worker(label, value)


def main() -> None:
    context = multiprocessing.get_context("spawn")
    configured_count = os.environ.get("PYRUST_CHILD_COUNT", "2")
    try:
        child_count = int(configured_count)
    except ValueError as error:
        raise ValueError("PYRUST_CHILD_COUNT must be an integer") from error
    if child_count not in {1, 2}:
        raise ValueError("PYRUST_CHILD_COUNT must be 1 or 2")
    worker_specs = (("process-A", 20), ("process-B", 40))
    workers = [
        context.Process(target=python_worker, args=spec)
        for spec in worker_specs[:child_count]
    ]
    for worker in workers:
        worker.start()
    registry = Path(os.environ["PYRUST_CHILD_REGISTRY"])
    deadline = time.monotonic() + 20
    while len(list(registry.glob("attached-*"))) < len(workers):
        if time.monotonic() >= deadline:
            raise RuntimeError("PyRust coordinator did not attach to all children")
        time.sleep(0.02)
    (registry / "release").touch()
    for worker in workers:
        worker.join()
        if worker.exitcode != 0:
            raise SystemExit(worker.exitcode)


if __name__ == "__main__":
    main()
