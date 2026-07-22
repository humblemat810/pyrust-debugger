"""Prove Rust async futures retain embedded async-Python stack context."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

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
RUST_BINARY = CARGO_TARGET_DIR / "debug" / "rust-outer-python-async"
RUST_SOURCE = RUST_FIXTURE / "src" / "async_main.rs"

CRITERIA = (
    "AC-RA-01",
    "AC-RA-02",
    "AC-RA-03",
    "AC-RA-04",
)


def _start_fixture() -> tuple[DapClient, dict[str, object]]:
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
            "pyrustPythonDebug": False,
        },
    )
    client.event("initialized", timeout=10)
    breakpoint = client.send(
        "setBreakpoints",
        {
            "source": {"path": str(RUST_SOURCE)},
            "breakpoints": [{"line": 15}],
            "sourceModified": False,
        },
    )
    client.response(breakpoint, timeout=10)
    configuration = client.send("configurationDone")
    client.response(configuration, timeout=10)
    client.response(launch, timeout=15)
    return client, client.event("stopped", timeout=20)


def _async_stop(
    client: DapClient,
    stopped: dict[str, object],
) -> tuple[int, int, str, str]:
    body = stopped.get("body") if isinstance(stopped, dict) else None
    thread_id = body.get("threadId") if isinstance(body, dict) else None
    if not isinstance(thread_id, int) or thread_id <= 0:
        raise DapError(f"Rust async stopped event has no thread ID: {stopped}")
    tree_request = client.send("pyrust/processTree")
    processes = client.response(tree_request, timeout=10).get("body", {}).get(
        "processes"
    )
    if not isinstance(processes, list):
        raise DapError(f"Rust async process tree was malformed: {processes}")
    stopped_thread_found = False
    for process in processes:
        if not isinstance(process, dict):
            raise DapError(f"Rust async process tree item was malformed: {process}")
        if any(key in process for key in ("tasks", "futures", "awaits", "children")):
            raise DapError(f"Rust async process tree invented task nesting: {process}")
        if not isinstance(process.get("threads"), list):
            raise DapError(f"Rust async process tree lacked native threads: {process}")
        for tree_thread in process["threads"]:
            if not isinstance(tree_thread, dict):
                raise DapError(f"Rust async tree thread was malformed: {tree_thread}")
            if any(key in tree_thread for key in ("children", "tasks", "futures", "awaits")):
                raise DapError(
                    f"Rust async tree thread invented task nesting: {tree_thread}"
                )
            if tree_thread.get("threadId") == thread_id:
                stopped_thread_found = tree_thread.get("isStopped") is True
    if not stopped_thread_found:
        raise DapError("Rust async process tree omitted the stopped native thread")
    request = client.send(
        "stackTrace",
        {"threadId": thread_id, "startFrame": 0, "levels": 100},
    )
    frames = client.response(request, timeout=15).get("body", {}).get("stackFrames")
    if not isinstance(frames, list):
        raise DapError(f"Rust async stack was malformed: {frames}")
    names = [str(frame.get("name", "")) for frame in frames]
    leaves = [name.rsplit("::", 1)[-1] for name in names]
    if leaves[:1] != ["rust_callback"] or not any("rust_outer" in name for name in names):
        raise DapError(f"Rust async native boundary was missing: {names[:24]}")
    try:
        python_frame = next(frame for frame in frames if frame.get("name") == "python_inner")
    except StopIteration as error:
        raise DapError(f"async Python frame was not merged: {names[:24]}") from error
    frame_id = python_frame.get("id")
    if not isinstance(frame_id, int):
        raise DapError(f"async Python frame has no synthetic ID: {python_frame}")
    locals_snapshot = _locals(client, frame_id)
    label = locals_snapshot.get("label")
    value = locals_snapshot.get("value")
    if (label, value) not in {
        ("'rust-async-A'", "20"),
        ("'rust-async-B'", "40"),
    }:
        raise DapError(f"Rust async locals were mismatched: {locals_snapshot}")
    if _evaluate_value(client, frame_id) != str(int(value) + 1):
        raise DapError(f"Rust async evaluation leaked: {locals_snapshot}")
    return thread_id, frame_id, label, value


def two_rust_async_happy_path() -> None:
    client, first_stop = _start_fixture()
    try:
        first_thread, first_frame, first_label, first_value = _async_stop(
            client,
            first_stop,
        )
        continued = client.send(
            "continue",
            {"threadId": first_thread, "singleThread": True},
        )
        client.response(continued, timeout=10)
        stale_request = client.send("scopes", {"frameId": first_frame})
        stale = client.wait_for(
            lambda message: message.get("type") == "response"
            and message.get("request_seq") == stale_request,
            timeout=10,
        )
        if stale.get("success", True):
            raise DapError("first Rust async frame survived continue")
        second_stop = client.event("stopped", timeout=20)
        second_thread, _, second_label, second_value = _async_stop(client, second_stop)
        if second_thread != first_thread:
            raise DapError("single-thread Rust async fixture changed OS thread")
        if {(first_label, first_value), (second_label, second_value)} != {
            ("'rust-async-A'", "20"),
            ("'rust-async-B'", "40"),
        }:
            raise DapError(
                "did not observe both Rust async snapshots: "
                f"{(first_label, first_value)!r}, {(second_label, second_value)!r}"
            )
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=CRITERIA)
    args = parser.parse_args()
    selected = (args.only,) if args.only else CRITERIA
    results = {criterion: False for criterion in selected}
    try:
        two_rust_async_happy_path()
        for criterion in selected:
            results[criterion] = True
    except (DapError, TimeoutError, OSError, ValueError) as error:
        print(f"Rust async acceptance: {error}", flush=True)

    for criterion in selected:
        print(f"{criterion} {'PASS' if results[criterion] else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
