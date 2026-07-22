"""Opt-in debugpy bootstrap injected into PyRust debuggee processes.

CPython imports ``sitecustomize`` during startup when this directory is present
on ``PYTHONPATH``. PyRust only enables the hook for launch configurations that
request full Python debugging, so normal Python processes are unaffected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time


_DEFAULT_ATTACH_TIMEOUT_SECONDS = 15.0


def _is_debugpy_internal_process() -> bool:
    """Do not bootstrap the private adapter process spawned by debugpy itself."""

    return any(
        "debugpy" in argument and ("adapter" in argument or "launcher" in argument)
        for argument in sys.argv
    )


def _is_main_interpreter() -> bool:
    """One process owns one debugpy server; secondary interpreters reuse it."""

    try:
        import _interpreters
    except ImportError:
        return True
    return _interpreters.get_current() == _interpreters.get_main()


def _write_endpoint(registry: Path, host: str, port: int) -> Path:
    process_id = os.getpid()
    payload = {
        "pid": process_id,
        "parentPid": os.getppid(),
        "host": host,
        "port": port,
        "threads": [
            {
                "pythonThreadId": thread.ident,
                "nativeThreadId": getattr(thread, "native_id", None),
                "name": thread.name,
            }
            for thread in threading.enumerate()
            if thread.ident is not None
        ],
    }
    target = registry / f"debugpy-{process_id}.json"
    target.with_suffix(".failed").unlink(missing_ok=True)
    target.with_suffix(".ready").unlink(missing_ok=True)
    temporary = target.with_name(f".{target.name}.{process_id}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, target)
    return target


def _attach_timeout_seconds() -> float:
    raw_value = os.environ.get("PYRUST_DEBUGPY_WAIT_TIMEOUT_SECONDS", "")
    if not raw_value:
        return _DEFAULT_ATTACH_TIMEOUT_SECONDS
    try:
        value = float(raw_value)
    except ValueError:
        return _DEFAULT_ATTACH_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_ATTACH_TIMEOUT_SECONDS


def _wait_for_client(
    debugpy_module: object,
    endpoint_record: Path,
    timeout_seconds: float,
) -> bool:
    failure_marker = endpoint_record.with_suffix(".failed")
    ready_marker = endpoint_record.with_suffix(".ready")
    deadline = time.monotonic() + timeout_seconds
    is_connected = getattr(debugpy_module, "is_client_connected")
    while time.monotonic() < deadline:
        if is_connected() and ready_marker.is_file():
            return True
        if failure_marker.is_file():
            return False
        time.sleep(0.025)
    return bool(is_connected() and ready_marker.is_file())


def _bootstrap() -> None:
    if os.environ.get("PYRUST_DEBUGPY_ENABLE") != "1":
        return
    registry_value = os.environ.get("PYRUST_DEBUGPY_REGISTRY")
    if (
        not registry_value
        or _is_debugpy_internal_process()
        or not _is_main_interpreter()
    ):
        return

    # A process has one debugpy server. Secondary interpreters must not start
    # another adapter or its daemon threads during their site initialization.
    import debugpy

    debugpy_python = os.environ.get("PYRUST_DEBUGPY_PYTHON")
    configure_options = {"subProcess": False}
    if debugpy_python:
        configure_options["python"] = debugpy_python
    debugpy.configure(configure_options)
    if debugpy_python and not sys.executable:
        # PyO3's embedded interpreter starts without sys.executable. debugpy's
        # normal adapter process needs it, while the in-process adapter breaks
        # CPython 3.14's external RemoteUnwinder at later Rust stops.
        sys.executable = debugpy_python
    host, port = debugpy.listen(("127.0.0.1", 0))
    registry = Path(registry_value)
    registry.mkdir(parents=True, exist_ok=True)
    endpoint_record = _write_endpoint(registry, host, port)
    if os.environ.get("PYRUST_DEBUGPY_WAIT_FOR_CLIENT") == "1":
        timeout_seconds = _attach_timeout_seconds()
        if not _wait_for_client(debugpy, endpoint_record, timeout_seconds):
            print(
                "PyRust debugpy attach did not complete; continuing without "
                "Python breakpoint support",
                file=sys.stderr,
                flush=True,
            )


_bootstrap()
