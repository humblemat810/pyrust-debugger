"""Prove a manual process/thread breakpoint survives the old fixture cutoff."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from .dap_support import DapError
from .run_process_thread_mode_acceptance import (
    _inspect_stop,
    _process_tree,
    _start_fixture,
    _wait_for_records,
)


DEFAULT_IDLE_HOLD_SECONDS = 65
HOLD_TIMEOUT_MARGIN_SECONDS = 60


def main() -> int:
    idle_seconds = int(
        os.environ.get("PYRUST_IDLE_ACCEPTANCE_SECONDS", DEFAULT_IDLE_HOLD_SECONDS)
    )
    if idle_seconds <= 45:
        raise ValueError("idle acceptance must exceed the old 45-second cutoff")

    with TemporaryDirectory(prefix="pyrust-process-thread-idle-") as directory:
        registry = Path(directory)
        client, stopped = _start_fixture(
            registry,
            breakpoint_hold_timeout_seconds=idle_seconds
            + HOLD_TIMEOUT_MARGIN_SECONDS,
        )
        try:
            records = _wait_for_records(registry)
            process_id, thread_id, _ = _inspect_stop(client, stopped, records)
            print(
                f"AC-PTM-IDLE holding pid={process_id} tid={thread_id} "
                f"for {idle_seconds}s",
                flush=True,
            )
            time.sleep(idle_seconds)
            if client.process.poll() is not None:
                raise DapError("DAP proxy exited while the breakpoint was idle")

            tree = _process_tree(client)
            process = next(
                (
                    item
                    for item in tree
                    if item.get("processId") == process_id
                ),
                None,
            )
            if not isinstance(process, dict) or not process.get("isStopped"):
                raise DapError(
                    "stopped process disappeared during the idle breakpoint hold"
                )
            _inspect_stop(client, stopped, records)
            print(
                f"AC-PTM-IDLE PASS ({idle_seconds}s beyond old cutoff)",
                flush=True,
            )
            return 0
        finally:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
