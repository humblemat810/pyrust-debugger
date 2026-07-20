"""Registered Python child with two named workers entering Rust."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "research" / "fixtures" / "python_outer"
COORDINATOR_TIMEOUT_SECONDS = 30.0
WORKER_TIMEOUT_SECONDS = 20.0
COMMAND_LIMIT = 160
VALUES_BY_LABEL = {"process-A": 20, "process-B": 40}


def _bounded_command_summary(label: str, value: int) -> str:
    summary = (
        f"{Path(sys.executable).name} tests/acceptance/process_thread_worker.py "
        f"{label} {value}"
    )
    return summary[:COMMAND_LIMIT]


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _process_record(
    process_id: int,
    label: str,
    value: int,
    threads: list[dict[str, Any]],
) -> dict[str, Any]:
    parent_process_id = os.getppid()
    return {
        # The ADR 0008 keys are the process-tree contract. The old aliases
        # keep this fixture usable while the existing registry reader migrates.
        "processId": process_id,
        "parentProcessId": parent_process_id,
        "pid": process_id,
        "parentPid": parent_process_id,
        "label": label,
        "role": "Python child process",
        "command": _bounded_command_summary(label, value),
        "isActive": True,
        "isStopped": False,
        "threads": threads,
    }


def _wait_for_file(path: Path, description: str) -> None:
    deadline = time.monotonic() + COORDINATOR_TIMEOUT_SECONDS
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError(description)
        time.sleep(0.02)


def _allow_coordinator_attach() -> None:
    """Let the separately launched child CodeLLDB adapter ptrace this process."""

    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(0x59616D61, ctypes.c_ulong(-1).value, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "could not allow PyRust child attachment")


def python_worker(
    label: str,
    value: int,
    worker_number: int,
    ready: threading.Barrier,
    calls_released: threading.Event,
    workers: list[dict[str, Any]],
    workers_lock: threading.Lock,
    workers_announced: threading.Event,
    first_worker_finished: threading.Event,
    results: dict[str, int],
    failures: list[BaseException],
) -> None:
    """Keep the Python frame live directly above pyrust_native.rust_outer."""

    worker_label = label
    worker_value = value
    worker_name = f"{label}-worker-{worker_number}"
    try:
        with workers_lock:
            workers.append(
                {
                    "threadId": threading.get_native_id(),
                    "name": worker_name,
                    "isStopped": False,
                }
            )
            if len(workers) == 2:
                workers_announced.set()
        ready.wait()
        if not calls_released.wait(WORKER_TIMEOUT_SECONDS):
            raise TimeoutError(f"{worker_name} was not released to enter Rust")
        if worker_number == 2 and not first_worker_finished.wait(WORKER_TIMEOUT_SECONDS):
            raise TimeoutError(f"{worker_name} was not released after worker 1")

        import pyrust_native

        result = pyrust_native.rust_outer(worker_value)
        if worker_number == 1:
            first_worker_finished.set()
        with workers_lock:
            results[worker_name] = result
    except BaseException as error:
        with workers_lock:
            failures.append(error)
        ready.abort()


def run_process_thread_worker(label: str, value: int) -> None:
    if VALUES_BY_LABEL.get(label) != value:
        raise ValueError("expected process-A 20 or process-B 40")

    registry = Path(os.environ["PYRUST_CHILD_REGISTRY"])
    registry.mkdir(parents=True, exist_ok=True)
    _allow_coordinator_attach()
    process_id = os.getpid()
    record_path = registry / f"child-{process_id}.json"
    _write_json_atomically(record_path, _process_record(process_id, label, value, []))
    (registry / f"ready-{process_id}").touch()
    _wait_for_file(
        registry / f"attached-{process_id}",
        "PyRust coordinator did not attach to process/thread child",
    )
    _wait_for_file(
        registry / "release",
        "PyRust coordinator did not release process/thread children",
    )

    workers: list[dict[str, Any]] = []
    workers_lock = threading.Lock()
    workers_announced = threading.Event()
    first_worker_finished = threading.Event()
    calls_released = threading.Event()
    ready = threading.Barrier(3, timeout=WORKER_TIMEOUT_SECONDS)
    results: dict[str, int] = {}
    failures: list[BaseException] = []
    threads = [
        threading.Thread(
            target=python_worker,
            args=(
                label,
                value,
                number,
                ready,
                calls_released,
                workers,
                workers_lock,
                workers_announced,
                first_worker_finished,
                results,
                failures,
            ),
            name=f"{label}-worker-{number}",
        )
        for number in (1, 2)
    ]

    try:
        for worker in threads:
            worker.start()
        if not workers_announced.wait(WORKER_TIMEOUT_SECONDS):
            raise TimeoutError(f"{label} workers did not publish native thread IDs")
        with workers_lock:
            announced_workers = sorted(workers, key=lambda worker: str(worker["name"]))
        _write_json_atomically(
            record_path,
            _process_record(process_id, label, value, announced_workers),
        )
        (registry / f"workers-ready-{process_id}").touch()
        ready.wait()
        calls_released.set()
    except BaseException:
        calls_released.set()
        ready.abort()
        raise
    finally:
        # A failure path above releases workers; this also prevents a join
        # from waiting on the call gate after a coordinator timeout.
        calls_released.set()
        for worker in threads:
            worker.join(WORKER_TIMEOUT_SECONDS)

    live_workers = [worker.name for worker in threads if worker.is_alive()]
    if live_workers:
        raise TimeoutError(f"{label} workers did not finish: {live_workers}")
    if failures:
        raise RuntimeError(f"{label} worker failed: {failures[0]!r}") from failures[0]
    expected = {f"{label}-worker-1", f"{label}-worker-2"}
    if set(results) != expected:
        raise RuntimeError(f"{label} worker results were incomplete: {results}")
    (registry / f"complete-{process_id}").touch()
    print(f"{label} thread results: {results}", flush=True)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: process_thread_worker.py LABEL VALUE")
    try:
        value = int(sys.argv[2])
    except ValueError as error:
        raise SystemExit("VALUE must be an integer") from error
    sys.path.insert(0, str(FIXTURE))
    run_process_thread_worker(sys.argv[1], value)


if __name__ == "__main__":
    main()
