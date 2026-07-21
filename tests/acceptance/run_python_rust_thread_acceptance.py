"""Prove Python entry can create separately visible Rust worker threads."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
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


ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "tests" / "acceptance" / "python_rust_thread_fixture_driver.py"

CRITERIA = (
    "AC-PRT-01",
    "AC-PRT-02",
    "AC-PRT-03",
    "AC-PRT-04",
)
EXPECTED_WORKERS = {
    "rust-child-20-1",
    "rust-child-20-2",
    "rust-child-40-1",
    "rust-child-40-2",
}


def _start_fixture() -> tuple[DapClient, dict[str, Any]]:
    client = DapClient(proxy_command())
    initialize(client)
    launch = client.send("launch", launch_arguments(program=PYTHON, args=[str(DRIVER)]))
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


def _stack(client: DapClient, thread_id: int) -> list[dict[str, Any]]:
    request = client.send(
        "stackTrace",
        {"threadId": thread_id, "startFrame": 0, "levels": 40},
    )
    frames = client.response(request, timeout=15).get("body", {}).get("stackFrames")
    if not isinstance(frames, list):
        raise DapError(f"Rust child stack was malformed: {frames}")
    return frames


def _worker_name(client: DapClient, thread_id: int) -> str:
    request = client.send("threads")
    threads = client.response(request, timeout=10).get("body", {}).get("threads")
    if not isinstance(threads, list):
        raise DapError(f"DAP threads response was malformed: {threads}")
    for thread in threads:
        if isinstance(thread, dict) and thread.get("id") == thread_id:
            name = thread.get("name")
            if isinstance(name, str):
                return _fixture_worker_name(name)
    raise DapError(f"stopped Rust worker was missing from DAP threads: {threads}")


def _fixture_worker_name(codelldb_name: str) -> str:
    match = re.search(r'"(rust-child-\d+-[12])"', codelldb_name)
    if match:
        return match.group(1)
    return codelldb_name


def _assert_rust_child_stop(client: DapClient, stopped: dict[str, Any]) -> int:
    thread_id = (stopped.get("body") or {}).get("threadId")
    if not isinstance(thread_id, int) or isinstance(thread_id, bool) or thread_id <= 0:
        raise DapError(f"stopped event has no Rust worker TID: {stopped}")
    name = _worker_name(client, thread_id)
    if name not in EXPECTED_WORKERS:
        raise DapError(f"stopped thread was not a named Rust child: {name!r}")

    frames = _stack(client, thread_id)
    names = [str(frame.get("name", "")) for frame in frames]
    if not names or not names[0].endswith("rust_inner"):
        raise DapError(f"Rust child did not stop in rust_inner: {names[:8]}")
    if not any("rust_outer_with_rust_threads" in name for name in names):
        raise DapError(f"Rust child lost its Python-to-Rust parent function: {names[:8]}")
    first = frames[0]
    source = first.get("source") if isinstance(first, dict) else None
    if not isinstance(source, dict) or not str(source.get("path", "")).endswith(
        "research/fixtures/python_outer/src/lib.rs"
    ):
        raise DapError(f"Rust child source was not python_outer/lib.rs: {first}")
    if first.get("line") != 6:
        raise DapError(f"Rust child stopped at the wrong line: {first}")
    return thread_id


def _assert_tree_matches_dap_threads(client: DapClient) -> None:
    threads_request = client.send("threads")
    dap_threads = client.response(threads_request, timeout=10).get("body", {}).get(
        "threads"
    )
    if not isinstance(dap_threads, list):
        raise DapError(f"DAP threads response was malformed: {dap_threads}")
    expected_ids = {
        thread["id"]
        for thread in dap_threads
        if isinstance(thread, dict) and isinstance(thread.get("id"), int)
    }
    tree_request = client.send("pyrust/processTree")
    processes = client.response(tree_request, timeout=10).get("body", {}).get(
        "processes"
    )
    if not isinstance(processes, list):
        raise DapError(f"process tree was malformed: {processes}")
    actual_ids = {
        thread["threadId"]
        for process in processes
        if isinstance(process, dict)
        for thread in process.get("threads", [])
        if isinstance(thread, dict) and isinstance(thread.get("threadId"), int)
    }
    if not expected_ids.issubset(actual_ids):
        raise DapError(
            "process tree omitted threads that the Call Stack can display: "
            f"missing {expected_ids - actual_ids}"
        )


def _assert_paused_sibling_disassembly(client: DapClient) -> None:
    threads_request = client.send("threads")
    threads = client.response(threads_request, timeout=10).get("body", {}).get(
        "threads"
    )
    if not isinstance(threads, list):
        raise DapError(f"DAP threads response was malformed: {threads}")

    disassembled = 0
    for thread in threads:
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, int):
            continue
        stack_request = client.send(
            "stackTrace",
            {"threadId": thread_id, "startFrame": 0, "levels": 1},
        )
        frames = client.response(stack_request, timeout=15).get("body", {}).get(
            "stackFrames"
        )
        if not isinstance(frames, list) or not frames:
            continue
        frame = frames[0] if isinstance(frames[0], dict) else {}
        source = frame.get("source") if isinstance(frame.get("source"), dict) else {}
        instruction_pointer = frame.get("instructionPointerReference")
        if source.get("path") or not isinstance(instruction_pointer, str):
            continue
        disassemble_request = client.send(
            "disassemble",
            {
                "memoryReference": instruction_pointer,
                "instructionOffset": -4,
                "instructionCount": 12,
                "resolveSymbols": True,
            },
        )
        instructions = client.response(disassemble_request, timeout=10).get(
            "body", {}
        ).get("instructions")
        if not isinstance(instructions, list) or not instructions:
            raise DapError(
                f"paused sibling {thread_id} returned no native disassembly"
            )
        disassembled += 1

    if disassembled == 0:
        raise DapError("no paused sibling exposed an instruction-backed frame")

    health_request = client.send("threads")
    if client.response(health_request, timeout=10).get("success") is not True:
        raise DapError("adapter became unusable after sibling disassembly")

    rejected_request = client.send(
        "disassemble",
        {
            "memoryReference": "0x1",
            "instructionOffset": 0,
            "instructionCount": 4,
            "resolveSymbols": True,
        },
    )
    rejected = client.wait_for(
        lambda message: message.get("type") == "response"
        and message.get("request_seq") == rejected_request,
        timeout=10,
    )
    if rejected.get("success") is not False:
        raise DapError("known-invalid disassembly unexpectedly succeeded")

    tree_request = client.send("pyrust/processTree")
    processes = client.response(tree_request, timeout=10).get("body", {}).get(
        "processes"
    )
    if not isinstance(processes, list) or not any(
        process.get("isStopped") is True
        for process in processes
        if isinstance(process, dict)
    ):
        raise DapError("failed disassembly changed the stopped process state")


def _assert_no_native_boundary_diagnostic(client: DapClient) -> None:
    messages = [*client.messages, *client.pending, *client.deferred]
    output = "\n".join(
        str((message.get("body") or {}).get("output", ""))
        for message in messages
        if message.get("type") == "event" and message.get("event") == "output"
    )
    if "native fixture boundary was not found" in output:
        raise DapError(f"native-only stack emitted a helper failure: {output}")


def python_threads_create_rust_threads() -> None:
    client, stopped = _start_fixture()
    try:
        _assert_tree_matches_dap_threads(client)
        _assert_paused_sibling_disassembly(client)
        seen: set[str] = set()
        for index in range(len(EXPECTED_WORKERS)):
            thread_id = _assert_rust_child_stop(client, stopped)
            _assert_no_native_boundary_diagnostic(client)
            name = _worker_name(client, thread_id)
            if name in seen:
                raise DapError(f"Rust worker stopped twice before all workers ran: {name}")
            seen.add(name)
            continue_request = client.send(
                "continue",
                {"threadId": thread_id, "singleThread": False},
            )
            client.response(continue_request, timeout=10)
            if index + 1 < len(EXPECTED_WORKERS):
                stopped = client.event("stopped", timeout=30)
        if seen != EXPECTED_WORKERS:
            raise DapError(f"did not observe each named Rust worker: {seen}")
        _assert_no_native_boundary_diagnostic(client)
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=CRITERIA)
    args = parser.parse_args()
    selected = (args.only,) if args.only else CRITERIA
    results = {criterion: False for criterion in selected}
    try:
        python_threads_create_rust_threads()
        for criterion in selected:
            results[criterion] = True
    except (DapError, TimeoutError, OSError, ValueError) as error:
        print(f"Python/Rust thread acceptance: {error}", flush=True)

    for criterion in selected:
        print(f"{criterion} {'PASS' if results[criterion] else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
