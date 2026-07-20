"""Prove asyncio tasks retain their active Python/Rust mixed stack context."""

from __future__ import annotations

import argparse
from pathlib import Path

from .dap_support import DapClient, DapError, PYTHON, RUST_SOURCE, launch_arguments, proxy_command
from .run_acceptance import initialize
from .run_thread_acceptance import _evaluate_value, _locals


ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "tests" / "acceptance" / "async_fixture_driver.py"

CRITERIA = (
    "AC-AT-01",
    "AC-AT-02",
    "AC-AT-03",
    "AC-AT-04",
)


def _start_fixture() -> tuple[DapClient, dict[str, object]]:
    client = DapClient(proxy_command())
    initialize(client)
    launch = client.send(
        "launch",
        launch_arguments(program=PYTHON, args=[str(DRIVER)]),
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
    return client, client.event("stopped", timeout=20)


def _task_stop(
    client: DapClient,
    stopped: dict[str, object],
) -> tuple[int, int, str, str]:
    body = stopped.get("body") if isinstance(stopped, dict) else None
    thread_id = body.get("threadId") if isinstance(body, dict) else None
    if not isinstance(thread_id, int) or thread_id <= 0:
        raise DapError(f"async stopped event has no thread ID: {stopped}")

    request = client.send(
        "stackTrace",
        {"threadId": thread_id, "startFrame": 0, "levels": 80},
    )
    frames = client.response(request, timeout=15).get("body", {}).get("stackFrames")
    if not isinstance(frames, list):
        raise DapError(f"async stack was malformed: {frames}")
    leaves = [str(frame.get("name", "")).rsplit("::", 1)[-1] for frame in frames]
    if leaves[:2] != ["rust_inner", "rust_outer"]:
        raise DapError(f"async task lost native Rust prefix: {leaves[:8]}")
    try:
        task_frame = next(frame for frame in frames if frame.get("name") == "async_worker")
    except StopIteration as error:
        raise DapError(f"async_worker was not merged: {leaves[:20]}") from error
    frame_id = task_frame.get("id")
    if not isinstance(frame_id, int):
        raise DapError(f"async worker frame has no synthetic ID: {task_frame}")
    locals_snapshot = _locals(client, frame_id)
    label = locals_snapshot.get("label")
    value = locals_snapshot.get("value")
    task_name = locals_snapshot.get("task_name")
    expected = {
        ("'async-A'", "20", "'async-A'"),
        ("'async-B'", "40", "'async-B'"),
    }
    if (label, value, task_name) not in expected:
        raise DapError(f"async task locals were mismatched: {locals_snapshot}")
    if _evaluate_value(client, frame_id) != str(int(value) + 1):
        raise DapError(f"async task evaluation leaked: {locals_snapshot}")
    return thread_id, frame_id, label, value


def two_async_task_happy_path() -> None:
    client, first_stop = _start_fixture()
    try:
        first_thread, first_frame, first_label, first_value = _task_stop(
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
            raise DapError("first async task frame survived continue")

        second_stop = client.event("stopped", timeout=20)
        second_thread, _, second_label, second_value = _task_stop(client, second_stop)
        if second_thread != first_thread:
            raise DapError("asyncio tasks unexpectedly used separate OS threads")
        if {(first_label, first_value), (second_label, second_value)} != {
            ("'async-A'", "20"),
            ("'async-B'", "40"),
        }:
            raise DapError(
                "did not observe both async task snapshots: "
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
        two_async_task_happy_path()
        for criterion in selected:
            results[criterion] = True
    except (DapError, TimeoutError, OSError, ValueError) as error:
        print(f"async acceptance: {error}", flush=True)

    for criterion in selected:
        print(f"{criterion} {'PASS' if results[criterion] else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
