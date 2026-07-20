"""Prove spawned Python children retain independent mixed Rust/Python stacks."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .dap_support import (
    DapClient,
    DapError,
    PYTHON,
    RUST_SOURCE,
    launch_arguments,
    proxy_command,
)
from .run_acceptance import initialize
from .run_thread_acceptance import _evaluate_value, _locals


ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "tests" / "acceptance" / "multiprocess_fixture_driver.py"

CRITERIA = (
    "AC-MP-01",
    "AC-MP-02",
    "AC-MP-03",
    "AC-MP-04",
    "AC-MP-05",
)


def _start_fixture(registry: Path) -> tuple[DapClient, dict[str, Any]]:
    client = DapClient(proxy_command())
    initialize(client)
    arguments = launch_arguments(program=PYTHON, args=[str(DRIVER)])
    arguments["env"] = {"PYRUST_CHILD_REGISTRY": str(registry)}
    arguments["pyrustChildRegistryPath"] = str(registry)
    arguments["pyrustProcessMode"] = "children"
    launch = client.send("launch", arguments)
    client.event("initialized", timeout=10)
    breakpoint_request = client.send(
        "setBreakpoints",
        {
            "source": {"path": str(RUST_SOURCE)},
            "breakpoints": [{"line": 6}],
            "sourceModified": False,
        },
    )
    client.response(breakpoint_request, timeout=10)
    configuration = client.send("configurationDone")
    client.response(configuration, timeout=10)
    client.response(launch, timeout=15)
    return client, client.event("stopped", timeout=30)


def _child_stop(
    client: DapClient,
    stopped: dict[str, Any],
) -> tuple[int, int, str, str]:
    body = stopped.get("body") or {}
    process_id = body.get("systemProcessId")
    thread_id = body.get("threadId")
    if not isinstance(process_id, int) or process_id <= 0:
        raise DapError(f"child stopped event has no process ID: {stopped}")
    if not isinstance(thread_id, int) or thread_id <= 0:
        raise DapError(f"child stopped event has no thread ID: {stopped}")

    threads_request = client.send("threads")
    threads = client.response(threads_request, timeout=10).get("body", {}).get(
        "threads"
    )
    if not isinstance(threads, list) or not any(
        item.get("id") == thread_id for item in threads if isinstance(item, dict)
    ):
        raise DapError(f"child thread was not exposed by coordinator: {threads}")

    stack_request = client.send(
        "stackTrace",
        {"threadId": thread_id, "startFrame": 0, "levels": 80},
    )
    frames = client.response(stack_request, timeout=15).get("body", {}).get(
        "stackFrames"
    )
    if not isinstance(frames, list):
        raise DapError(f"child stack was malformed: {frames}")
    leaves = [str(frame.get("name", "")).rsplit("::", 1)[-1] for frame in frames]
    if leaves[:2] != ["rust_inner", "rust_outer"]:
        raise DapError(f"child stack lost the Rust boundary: {leaves[:8]}")
    try:
        python_frame = next(frame for frame in frames if frame.get("name") == "python_worker")
    except StopIteration as error:
        raise DapError(f"child stack lost python_worker: {leaves[:20]}") from error
    frame_id = python_frame.get("id")
    if not isinstance(frame_id, int):
        raise DapError(f"child Python frame lacks a synthetic ID: {python_frame}")
    locals_snapshot = _locals(client, frame_id)
    label = locals_snapshot.get("label")
    value = locals_snapshot.get("value")
    if (label, value) not in {
        ("'process-A'", "20"),
        ("'process-B'", "40"),
    }:
        raise DapError(f"child Python locals were mismatched: {locals_snapshot}")
    if _evaluate_value(client, frame_id) != str(int(value) + 1):
        raise DapError(f"child Python evaluation was incorrect: {locals_snapshot}")

    rust_frame = frames[0].get("id")
    if not isinstance(rust_frame, int):
        raise DapError(f"child Rust frame lacks a virtual ID: {frames[0]}")
    native_evaluate = client.send(
        "evaluate",
        {"expression": "value", "frameId": rust_frame, "context": "watch"},
    )
    result = str(client.response(native_evaluate, timeout=10).get("body", {}).get("result"))
    if result not in {"20", "40"}:
        raise DapError(f"child Rust evaluation was incorrect: {result!r}")
    return process_id, thread_id, label, value


def _assert_process_tree(
    client: DapClient,
    *,
    expected_parent: int,
    child_processes: set[int],
) -> None:
    request = client.send("pyrust/processTree")
    processes = client.response(request, timeout=10).get("body", {}).get("processes")
    if not isinstance(processes, list):
        raise DapError(f"process-tree payload was malformed: {processes}")
    by_pid = {
        item.get("processId"): item
        for item in processes
        if isinstance(item, dict) and isinstance(item.get("processId"), int)
    }
    if expected_parent not in by_pid:
        raise DapError(f"process tree omitted parent {expected_parent}: {processes}")
    for child_process in child_processes:
        child = by_pid.get(child_process)
        if not isinstance(child, dict) or child.get("parentProcessId") != expected_parent:
            raise DapError(
                f"process tree did not nest child {child_process}: {processes}"
            )


def two_child_happy_path() -> None:
    with TemporaryDirectory(prefix="pyrust-child-registry-") as directory:
        registry = Path(directory)
        client, first_stop = _start_fixture(registry)
        try:
            first_process, first_thread, first_label, first_value = _child_stop(
                client, first_stop
            )
            registry_records = list(registry.glob("child-*.json"))
            if len(registry_records) != 2:
                raise DapError(f"fixture did not register two children: {registry_records}")
            expected_parent = __import__("json").loads(
                registry_records[0].read_text(encoding="utf-8")
            )["parentPid"]
            _assert_process_tree(
                client,
                expected_parent=expected_parent,
                child_processes={
                    int(record.stem.partition("-")[2]) for record in registry_records
                },
            )
            first_frames_request = client.send(
                "stackTrace",
                {"threadId": first_thread, "startFrame": 0, "levels": 80},
            )
            first_frames = client.response(first_frames_request, timeout=15).get(
                "body", {}
            ).get("stackFrames", [])
            first_python_frame = next(
                frame["id"]
                for frame in first_frames
                if frame.get("name") == "python_worker"
            )

            continue_first = client.send(
                "continue", {"threadId": first_thread, "singleThread": False}
            )
            client.response(continue_first, timeout=10)
            stale_request = client.send("scopes", {"frameId": first_python_frame})
            stale = client.wait_for(
                lambda message: message.get("type") == "response"
                and message.get("request_seq") == stale_request,
                timeout=10,
            )
            if stale.get("success", True):
                raise DapError("child synthetic Python frame survived continue")

            second_stop = client.event("stopped", timeout=30)
            second_process, second_thread, second_label, second_value = _child_stop(
                client, second_stop
            )
            if first_process == second_process:
                raise DapError("two child stops reused one process identity")
            if first_thread == second_thread:
                raise DapError("two child stops reused one thread identity")
            if {(first_label, first_value), (second_label, second_value)} != {
                ("'process-A'", "20"),
                ("'process-B'", "40"),
            }:
                raise DapError(
                    "did not observe both child local snapshots: "
                    f"{(first_label, first_value)!r}, {(second_label, second_value)!r}"
                )
            continue_second = client.send(
                "continue", {"threadId": second_thread, "singleThread": False}
            )
            client.response(continue_second, timeout=10)
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
        print(f"Multiprocess acceptance: {error}", flush=True)

    for criterion in selected:
        print(f"{criterion} {'PASS' if results[criterion] else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
