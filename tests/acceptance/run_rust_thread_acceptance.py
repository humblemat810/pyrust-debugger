"""Prove two Rust workers retain independent embedded-Python stacks."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from .dap_support import DapClient, DapError, ROOT
from .run_reverse_acceptance import initialize, python_libdir, proxy_command
from .run_thread_acceptance import _evaluate_value, _locals


RUST_FIXTURE = ROOT / "research" / "fixtures" / "rust_outer"
_configured_target = os.environ.get("CARGO_TARGET_DIR")
CARGO_TARGET_DIR = (
    Path(_configured_target) if _configured_target else RUST_FIXTURE / "target"
)
if not CARGO_TARGET_DIR.is_absolute():
    CARGO_TARGET_DIR = ROOT / CARGO_TARGET_DIR
RUST_BINARY = CARGO_TARGET_DIR / "debug" / "rust-outer-python-threads"
RUST_SOURCE = RUST_FIXTURE / "src" / "threaded_main.rs"

CRITERIA = (
    "AC-RT-01",
    "AC-RT-02",
    "AC-RT-03",
    "AC-RT-04",
)


def _start_fixture() -> tuple[DapClient, dict[str, Any]]:
    client = DapClient(proxy_command())
    initialize(client)
    launch = client.send(
        "launch",
        {
            "program": str(RUST_BINARY),
            "args": [],
            "cwd": str(ROOT),
            "env": {"LD_LIBRARY_PATH": python_libdir()},
            "terminal": "console",
            "consoleMode": "evaluate",
            "sourceLanguages": ["rust"],
        },
    )
    client.event("initialized", timeout=10)
    breakpoints = client.send(
        "setBreakpoints",
        {
            "source": {"path": str(RUST_SOURCE)},
            "breakpoints": [{"line": 13}],
            "sourceModified": False,
        },
    )
    client.response(breakpoints, timeout=10)
    configuration = client.send("configurationDone")
    client.response(configuration, timeout=10)
    client.response(launch, timeout=15)
    return client, client.event("stopped", timeout=20)


def _worker_stop(
    client: DapClient,
    stopped: dict[str, Any],
) -> tuple[int, int, str, str]:
    thread_id = (stopped.get("body") or {}).get("threadId")
    if not isinstance(thread_id, int) or isinstance(thread_id, bool) or thread_id <= 0:
        raise DapError(f"stopped event has no usable Rust worker ID: {stopped}")

    threads_request = client.send("threads")
    threads = client.response(threads_request, timeout=10).get("body", {}).get(
        "threads"
    )
    if not isinstance(threads, list) or not any(
        thread.get("id") == thread_id
        for thread in threads
        if isinstance(thread, dict)
    ):
        raise DapError(f"CodeLLDB did not report the stopped Rust worker: {threads}")

    stack_request = client.send(
        "stackTrace",
        {"threadId": thread_id, "startFrame": 0, "levels": 100},
    )
    frames = client.response(stack_request, timeout=15).get("body", {}).get(
        "stackFrames"
    )
    if not isinstance(frames, list):
        raise DapError(f"Rust worker stack was malformed: {frames}")
    leaves = [str(frame.get("name", "")).rsplit("::", 1)[-1] for frame in frames]
    if leaves[:1] != ["rust_callback"] or "rust_outer" not in leaves:
        raise DapError(f"Rust worker native boundary was missing: {leaves[:24]}")
    try:
        python_frame = next(frame for frame in frames if frame.get("name") == "python_inner")
    except StopIteration as error:
        raise DapError(f"embedded Python frame was not merged: {leaves[:24]}") from error
    frame_id = python_frame.get("id")
    if not isinstance(frame_id, int):
        raise DapError(f"embedded Python frame has no synthetic ID: {python_frame}")
    locals_snapshot = _locals(client, frame_id)
    label = locals_snapshot.get("label")
    value = locals_snapshot.get("value")
    if (label, value) not in {
        ("'rust-worker-A'", "20"),
        ("'rust-worker-B'", "40"),
    }:
        raise DapError(f"Rust worker locals were mismatched: {locals_snapshot}")
    if _evaluate_value(client, frame_id) != str(int(value) + 1):
        raise DapError(f"Rust worker evaluation leaked: {locals_snapshot}")
    return thread_id, frame_id, label, value


def two_rust_worker_happy_path() -> None:
    client, first_stop = _start_fixture()
    try:
        first_thread, first_frame, first_label, first_value = _worker_stop(
            client, first_stop
        )
        continue_request = client.send(
            "continue", {"threadId": first_thread, "singleThread": True}
        )
        client.response(continue_request, timeout=10)

        stale_request = client.send("scopes", {"frameId": first_frame})
        stale = client.wait_for(
            lambda message: message.get("type") == "response"
            and message.get("request_seq") == stale_request,
            timeout=10,
        )
        if stale.get("success", True):
            raise DapError("first Rust worker Python frame stayed valid after continue")

        second_stop = client.event("stopped", timeout=20)
        second_thread, _, second_label, second_value = _worker_stop(client, second_stop)
        if second_thread == first_thread:
            raise DapError("second Rust callback reused the first worker thread ID")
        if {(first_label, first_value), (second_label, second_value)} != {
            ("'rust-worker-A'", "20"),
            ("'rust-worker-B'", "40"),
        }:
            raise DapError(
                "did not observe both Rust worker snapshots: "
                f"{(first_label, first_value)!r}, {(second_label, second_value)!r}"
            )
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=CRITERIA)
    args = parser.parse_args()
    selected = (args.only,) if args.only else CRITERIA
    results: dict[str, bool] = {criterion: False for criterion in selected}
    try:
        two_rust_worker_happy_path()
        for criterion in selected:
            results[criterion] = True
    except (DapError, TimeoutError, OSError, ValueError) as error:
        print(f"Rust-threaded acceptance: {error}", flush=True)

    for criterion in selected:
        print(f"{criterion} {'PASS' if results[criterion] else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
