"""Prove one Python process can expose two independent Python/Rust workers."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .dap_support import DapClient, DapError, PYTHON, RUST_SOURCE, launch_arguments, proxy_command
from .run_acceptance import initialize


ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "tests" / "acceptance" / "threaded_fixture_driver.py"

CRITERIA = (
    "AC-MT-01",
    "AC-MT-02",
    "AC-MT-03",
    "AC-MT-04",
)


def _start_fixture() -> tuple[DapClient, dict[str, Any]]:
    client = DapClient(proxy_command())
    initialize(client)
    launch = client.send(
        "launch",
        launch_arguments(program=PYTHON, args=[str(DRIVER)]),
    )
    client.event("initialized")
    breakpoints = client.send(
        "setBreakpoints",
        {
            "source": {"path": str(RUST_SOURCE)},
            "breakpoints": [{"line": 6}],
            "sourceModified": False,
        },
    )
    client.response(breakpoints)
    configuration = client.send("configurationDone")
    client.response(configuration)
    client.response(launch)
    return client, client.event("stopped", timeout=20)


def _stack(client: DapClient, thread_id: int) -> list[dict[str, Any]]:
    request = client.send(
        "stackTrace",
        {"threadId": thread_id, "startFrame": 0, "levels": 40},
    )
    response = client.response(request, timeout=15)
    frames = response.get("body", {}).get("stackFrames")
    if not isinstance(frames, list):
        raise DapError(f"thread stack was malformed: {response}")
    return frames


def _locals(client: DapClient, frame_id: int) -> dict[str, str]:
    scopes_request = client.send("scopes", {"frameId": frame_id})
    scopes = client.response(scopes_request, timeout=10).get("body", {}).get("scopes")
    if not isinstance(scopes, list) or len(scopes) != 1:
        raise DapError(f"Python worker scopes were malformed: {scopes}")
    reference = scopes[0].get("variablesReference")
    if reference != frame_id:
        raise DapError(f"Python worker scope reference was malformed: {scopes}")
    variables_request = client.send("variables", {"variablesReference": reference})
    variables = client.response(variables_request, timeout=10).get("body", {}).get(
        "variables"
    )
    if not isinstance(variables, list):
        raise DapError(f"Python worker locals were malformed: {variables}")
    return {
        str(variable.get("name")): str(variable.get("value"))
        for variable in variables
        if isinstance(variable, dict)
    }


def _evaluate_value(client: DapClient, frame_id: int) -> str:
    request = client.send(
        "evaluate",
        {"expression": "value + 1", "frameId": frame_id, "context": "watch"},
    )
    return str(client.response(request, timeout=10).get("body", {}).get("result"))


def _worker_stop(
    client: DapClient,
    stopped: dict[str, Any],
) -> tuple[int, int, str, str]:
    thread_id = (stopped.get("body") or {}).get("threadId")
    if not isinstance(thread_id, int) or isinstance(thread_id, bool) or thread_id <= 0:
        raise DapError(f"stopped event has no usable worker thread ID: {stopped}")

    threads_request = client.send("threads")
    thread_list = client.response(threads_request, timeout=10).get("body", {}).get(
        "threads"
    )
    if not isinstance(thread_list, list) or not any(
        thread.get("id") == thread_id
        for thread in thread_list
        if isinstance(thread, dict)
    ):
        raise DapError(f"CodeLLDB did not report the stopped worker: {thread_list}")

    frames = _stack(client, thread_id)
    names = [str(frame.get("name", "")).rsplit("::", 1)[-1] for frame in frames]
    if names[:2] != ["rust_inner", "rust_outer"]:
        raise DapError(f"worker lost native Rust stack prefix: {names[:4]}")
    try:
        worker_frame = next(frame for frame in frames if frame.get("name") == "python_worker")
    except StopIteration as error:
        raise DapError(f"worker Python frame was not merged: {names}") from error
    frame_id = worker_frame.get("id")
    if not isinstance(frame_id, int):
        raise DapError(f"worker Python frame has no synthetic ID: {worker_frame}")
    locals_snapshot = _locals(client, frame_id)
    label = locals_snapshot.get("label")
    value = locals_snapshot.get("value")
    if label not in {"'worker-A'", "'worker-B'"} or value not in {"20", "40"}:
        raise DapError(f"worker locals were not fixture values: {locals_snapshot}")
    if (label, value) not in {("'worker-A'", "20"), ("'worker-B'", "40")}:
        raise DapError(f"worker label/value were cross-thread inconsistent: {locals_snapshot}")
    evaluated = _evaluate_value(client, frame_id)
    if evaluated != str(int(value) + 1):
        raise DapError(
            f"worker evaluation leaked or was incorrect: {evaluated!r} for {locals_snapshot}"
        )
    return thread_id, frame_id, label, value


def two_worker_happy_path() -> None:
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
        stale_response = client.wait_for(
            lambda message: message.get("type") == "response"
            and message.get("request_seq") == stale_request,
            timeout=10,
        )
        if stale_response.get("success", True):
            raise DapError("first worker's synthetic frame stayed valid after continue")

        second_stop = client.event("stopped", timeout=20)
        second_thread, _, second_label, second_value = _worker_stop(client, second_stop)
        if second_thread == first_thread:
            raise DapError("second Rust worker stop reused the first worker thread ID")
        if (first_label, first_value) == (second_label, second_value):
            raise DapError("the two worker stops exposed the same Python local snapshot")
        if {(first_label, first_value), (second_label, second_value)} != {
            ("'worker-A'", "20"),
            ("'worker-B'", "40"),
        }:
            raise DapError(
                "did not observe both fixture workers: "
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
        two_worker_happy_path()
        for criterion in selected:
            results[criterion] = True
    except (DapError, TimeoutError, OSError, ValueError) as error:
        print(f"threaded acceptance: {error}", flush=True)

    for criterion in selected:
        print(f"{criterion} {'PASS' if results[criterion] else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
