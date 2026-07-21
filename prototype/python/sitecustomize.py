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


def _is_debugpy_internal_process() -> bool:
    """Do not bootstrap the private adapter process spawned by debugpy itself."""

    return any(
        "debugpy" in argument and ("adapter" in argument or "launcher" in argument)
        for argument in sys.argv
    )


def _write_endpoint(registry: Path, host: str, port: int) -> None:
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
    temporary = target.with_name(f".{target.name}.{process_id}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, target)


def _bootstrap() -> None:
    if os.environ.get("PYRUST_DEBUGPY_ENABLE") != "1":
        return
    registry_value = os.environ.get("PYRUST_DEBUGPY_REGISTRY")
    if not registry_value or _is_debugpy_internal_process():
        return

    # Child interpreters register independently, so debugpy must not inject a
    # second server through its own subprocess support.
    import debugpy

    debugpy.configure({"subProcess": False})
    host, port = debugpy.listen(("127.0.0.1", 0))
    registry = Path(registry_value)
    registry.mkdir(parents=True, exist_ok=True)
    _write_endpoint(registry, host, port)
    if os.environ.get("PYRUST_DEBUGPY_WAIT_FOR_CLIENT") == "1":
        debugpy.wait_for_client()


_bootstrap()
