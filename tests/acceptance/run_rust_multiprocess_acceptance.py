"""Prove a Rust parent can coordinate two Python/Rust child processes."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .dap_support import DapClient, DapError, PYTHON, ROOT, RUST_SOURCE, proxy_command
from .run_acceptance import initialize
from .run_multiprocess_acceptance import _child_stop


RUST_FIXTURE = ROOT / "research" / "fixtures" / "rust_outer"
_configured_target = os.environ.get("CARGO_TARGET_DIR")
CARGO_TARGET_DIR = (
    Path(_configured_target) if _configured_target else RUST_FIXTURE / "target"
)
if not CARGO_TARGET_DIR.is_absolute():
    CARGO_TARGET_DIR = ROOT / CARGO_TARGET_DIR
RUST_BINARY = CARGO_TARGET_DIR / "debug" / "rust-outer-python-processes"
WORKER = ROOT / "tests" / "acceptance" / "multiprocess_worker.py"

CRITERIA = (
    "AC-RMP-01",
    "AC-RMP-02",
    "AC-RMP-03",
    "AC-RMP-04",
    "AC-RMP-05",
)


def _start_fixture(registry: Path) -> tuple[DapClient, dict[str, Any]]:
    client = DapClient(proxy_command())
    initialize(client)
    launch = client.send(
        "launch",
        {
            "program": str(RUST_BINARY),
            "args": [],
            "cwd": str(ROOT),
            "env": {
                "PYRUST_CHILD_REGISTRY": str(registry),
                "PYRUST_PYTHON": str(PYTHON),
                "PYRUST_PROCESS_WORKER": str(WORKER),
            },
            "pyrustChildRegistryPath": str(registry),
            "pyrustProcessMode": "children",
        },
    )
    client.event("initialized", timeout=10)
    breakpoints = client.send(
        "setBreakpoints",
        {
            "source": {"path": str(RUST_SOURCE)},
            "breakpoints": [{"line": 6}],
            "sourceModified": False,
        },
    )
    client.response(breakpoints, timeout=10)
    configuration = client.send("configurationDone")
    client.response(configuration, timeout=10)
    client.response(launch, timeout=15)
    return client, client.event("stopped", timeout=30)


def two_child_happy_path() -> None:
    with TemporaryDirectory(prefix="pyrust-rust-parent-registry-") as directory:
        registry = Path(directory)
        client, first_stop = _start_fixture(registry)
        try:
            first_process, first_thread, first_label, first_value = _child_stop(
                client, first_stop
            )
            stack_request = client.send(
                "stackTrace",
                {"threadId": first_thread, "startFrame": 0, "levels": 80},
            )
            frames = client.response(stack_request, timeout=15).get("body", {}).get(
                "stackFrames", []
            )
            first_python_frame = next(
                frame["id"] for frame in frames if frame.get("name") == "python_worker"
            )
            continued = client.send(
                "continue", {"threadId": first_thread, "singleThread": False}
            )
            client.response(continued, timeout=10)
            stale_request = client.send("scopes", {"frameId": first_python_frame})
            stale = client.wait_for(
                lambda message: message.get("type") == "response"
                and message.get("request_seq") == stale_request,
                timeout=10,
            )
            if stale.get("success", True):
                raise DapError("Rust-parent child frame survived continue")

            second_stop = client.event("stopped", timeout=30)
            second_process, second_thread, second_label, second_value = _child_stop(
                client, second_stop
            )
            if first_process == second_process or first_thread == second_thread:
                raise DapError("Rust parent reused a child process/thread identity")
            if {(first_label, first_value), (second_label, second_value)} != {
                ("'process-A'", "20"),
                ("'process-B'", "40"),
            }:
                raise DapError(
                    "Rust parent did not expose both child local snapshots: "
                    f"{(first_label, first_value)!r}, {(second_label, second_value)!r}"
                )
            continued = client.send(
                "continue", {"threadId": second_thread, "singleThread": False}
            )
            client.response(continued, timeout=10)
        finally:
            client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=CRITERIA)
    args = parser.parse_args()
    selected = (args.only,) if args.only else CRITERIA
    results: dict[str, bool] = {criterion: False for criterion in selected}
    try:
        two_child_happy_path()
        for criterion in selected:
            results[criterion] = True
    except (DapError, TimeoutError, OSError, ValueError) as error:
        print(f"Rust-parent multiprocess acceptance: {error}", flush=True)

    for criterion in selected:
        print(f"{criterion} {'PASS' if results[criterion] else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
