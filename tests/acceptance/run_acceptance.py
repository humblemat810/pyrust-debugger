"""Run the complete first-workable-slice acceptance contract."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable

from .dap_support import (
    DapClient,
    DapError,
    PYTHON_SOURCE,
    RUST_SOURCE,
    launch_arguments,
    names,
    proxy_command,
    user_frames,
)


CRITERIA = (
    "AC-HP-01",
    "AC-HP-02",
    "AC-HP-03",
    "AC-HP-04",
    "AC-HP-05",
    "AC-SP-01",
    "AC-SP-02",
    "AC-SP-03",
    "AC-SP-04",
)


def initialize(client: DapClient) -> None:
    request = client.send(
        "initialize",
        {
            "clientID": "pyrust-acceptance",
            "adapterID": "pyrust",
            "pathFormat": "path",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "supportsRunInTerminalRequest": False,
        },
    )
    client.response(request)


def start_fixture(
    *,
    helper_command: str | None = None,
    helper_timeout_ms: int | None = None,
) -> tuple[DapClient, dict[str, Any]]:
    client = DapClient(proxy_command())
    initialize(client)
    launch = client.send(
        "launch",
        launch_arguments(
            helper_command=helper_command,
            helper_timeout_ms=helper_timeout_ms,
        ),
    )
    client.event("initialized")
    breakpoint_request = client.send(
        "setBreakpoints",
        {
            "source": {"path": str(RUST_SOURCE)},
            "breakpoints": [{"line": 6}],
            "sourceModified": False,
        },
    )
    client.response(breakpoint_request)
    configuration = client.send("configurationDone")
    client.response(configuration)
    client.response(launch)
    stopped = client.event("stopped", timeout=15)
    return client, stopped


def stack(client: DapClient, thread_id: int) -> dict[str, Any]:
    request = client.send(
        "stackTrace",
        {"threadId": thread_id, "startFrame": 0, "levels": 40},
    )
    return client.response(request, timeout=10)


def output_text(client: DapClient) -> str:
    return "\n".join(
        message.get("body", {}).get("output", "")
        for message in client.messages
        if message.get("type") == "event" and message.get("event") == "output"
    )


def assert_source_frames(frames: list[dict[str, Any]]) -> None:
    expected = {
        "rust_inner": (RUST_SOURCE, 6, "research/fixtures/python_outer/src/lib.rs"),
        "rust_outer": (RUST_SOURCE, 13, "research/fixtures/python_outer/src/lib.rs"),
        "python_inner": (PYTHON_SOURCE, 5, "research/fixtures/python_outer/app.py"),
        "python_outer": (PYTHON_SOURCE, 9, "research/fixtures/python_outer/app.py"),
    }
    for frame in frames:
        name = frame["name"].rsplit("::", 1)[-1]
        if name not in expected:
            continue
        source = frame.get("source") or {}
        path = source.get("path", "")
        if path != str(expected[name][0]) and not path.endswith(expected[name][2]):
            raise DapError(f"{name} has unexpected source path: {path!r}")
        if frame.get("line") != expected[name][1]:
            raise DapError(
                f"{name} has line {frame.get('line')}, expected {expected[name][1]}"
            )


def happy_path() -> dict[str, Any]:
    client, stopped = start_fixture()
    try:
        thread_id = stopped["body"]["threadId"]
        first_response = stack(client, thread_id)
        first_frames = user_frames(first_response)
        if names(first_frames) != [
            "rust_inner",
            "rust_outer",
            "python_inner",
            "python_outer",
        ]:
            raise DapError(f"unexpected mixed stack: {names(first_frames)}")
        if first_response.get("body", {}).get("totalFrames", 0) < len(first_frames):
            raise DapError("stackTrace totalFrames is smaller than returned frames")
        assert_source_frames(first_frames)

        native_id = next(
            frame["id"]
            for frame in first_frames
            if frame["name"].rsplit("::", 1)[-1] == "rust_inner"
        )
        scopes_request = client.send(
            "scopes", {"frameId": native_id}
        )
        scopes_response = client.response(scopes_request)
        if not isinstance(scopes_response.get("body", {}).get("scopes"), list):
            raise DapError("native scopes response is malformed")
        evaluate_request = client.send(
            "evaluate",
            {"expression": "value", "frameId": native_id, "context": "watch"},
        )
        evaluate_response = client.response(evaluate_request)
        result = str(evaluate_response.get("body", {}).get("result", ""))
        if not re.search(r"\b20\b", result):
            raise DapError(
                f"native evaluation did not return value 20: {evaluate_response}"
            )

        old_python_ids = {
            frame["id"]
            for frame in first_frames
            if frame["name"].rsplit("::", 1)[-1].startswith("python_")
        }
        continue_request = client.send(
            "continue", {"threadId": thread_id, "singleThread": True}
        )
        client.response(continue_request)
        client.event("stopped", timeout=15)
        second_frames = user_frames(stack(client, thread_id))
        new_python_ids = {
            frame["id"]
            for frame in second_frames
            if frame["name"].rsplit("::", 1)[-1].startswith("python_")
        }
        if not old_python_ids or not new_python_ids:
            raise DapError("synthetic Python frames were not returned at both stops")
        if old_python_ids & new_python_ids:
            raise DapError("synthetic Python frame IDs were reused across stops")
        stale_request = client.send(
            "scopes", {"frameId": next(iter(old_python_ids))}
        )
        stale_response = client.wait_for(
            lambda message: message.get("type") == "response"
            and message.get("request_seq") == stale_request,
            timeout=5,
        )
        if stale_response.get("success", True):
            raise DapError("stale synthetic frame ID was accepted")
        return {"first": first_frames, "second": second_frames}
    finally:
        client.close()


def fallback_path(helper_command: str, timeout_ms: int | None) -> None:
    client, stopped = start_fixture(
        helper_command=helper_command,
        helper_timeout_ms=timeout_ms,
    )
    try:
        started = time.monotonic()
        stack_response = stack(client, stopped["body"]["threadId"])
        frames = user_frames(stack_response)
        elapsed = time.monotonic() - started
        if any(frame["name"].rsplit("::", 1)[-1].startswith("python_") for frame in frames):
            raise DapError("helper failure returned synthetic Python frames")
        if names(frames) != ["rust_inner", "rust_outer"]:
            raise DapError(f"helper fallback changed native user frames: {names(frames)}")
        if not all(
            isinstance(frame.get("id"), int)
            and frame.get("instructionPointerReference")
            for frame in frames
        ):
            raise DapError("helper fallback did not preserve native frame identity")
        if timeout_ms is not None and elapsed > 2:
            raise DapError(f"helper timeout fallback took {elapsed:.2f}s")
        diagnostics = [
            message.get("body", {}).get("output", "").strip()
            for message in client.messages
            if message.get("type") == "event"
            and message.get("event") == "output"
            and any(
                token in message.get("body", {}).get("output", "").lower()
                for token in ("helper", "unwind")
            )
        ]
        if len(diagnostics) != 1:
            raise DapError(
                f"helper fallback expected one concise diagnostic, got {diagnostics}"
            )
        threads_request = client.send("threads")
        threads_response = client.response(threads_request)
        if not threads_response.get("body", {}).get("threads"):
            raise DapError("debug session was not usable after helper fallback")
    finally:
        client.close()


def synthetic_scopes() -> None:
    client, stopped = start_fixture()
    try:
        frames = user_frames(stack(client, stopped["body"]["threadId"]))
        python_id = next(
            frame["id"]
            for frame in frames
            if frame["name"].rsplit("::", 1)[-1] == "python_inner"
        )
        request = client.send("scopes", {"frameId": python_id})
        response = client.response(request)
        if response.get("body", {}).get("scopes") != []:
            raise DapError("synthetic Python scopes were not empty")
    finally:
        client.close()


def clean_protocol_failure() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        request = client.send(
            "launch",
            {
                "program": "/definitely/not/a/real/program",
                "args": [],
                "cwd": ".",
                "terminal": "console",
            },
        )
        response = client.wait_for(
            lambda message: message.get("type") == "response"
            and message.get("request_seq") == request,
            timeout=10,
        )
        if response.get("success", True):
            raise DapError("invalid downstream launch unexpectedly succeeded")
        message = json.dumps(response).lower()
        if not any(word in message for word in ("launch", "program", "adapter", "debug")):
            raise DapError("protocol failure lacked a layer-specific diagnostic")
    finally:
        client.close()


def run_case(label: str, action: Callable[[], Any], details: list[str]) -> bool:
    try:
        action()
        return True
    except Exception as error:
        details.append(f"{label}: {error}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit-only", action="store_true")
    args = parser.parse_args()
    details: list[str] = []
    status = {criterion: False for criterion in CRITERIA}

    unit_ok = True
    unit_environment = os.environ.copy()
    unit_environment["PYTHONPATH"] = os.pathsep.join(
        ["prototype/python", ".", unit_environment.get("PYTHONPATH", "")]
    )
    unit_suites = (
        ("adapter", ["-s", "prototype/adapter/tests"]),
        ("helper", ["-s", "prototype/python/tests"]),
        ("acceptance", ["-s", "tests/acceptance", "-t", "."]),
    )
    for label, discovery_arguments in unit_suites:
        try:
            unit = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    *discovery_arguments,
                ],
                capture_output=True,
                env=unit_environment,
                text=True,
                timeout=30,
            )
            if unit.returncode:
                unit_ok = False
                details.append(
                    f"{label} tests failed:\n{unit.stdout}{unit.stderr}"
                )
        except Exception as error:
            unit_ok = False
            details.append(f"{label} tests could not run: {error}")
    if args.unit_only:
        for criterion in CRITERIA:
            print(f"{criterion} {'PASS' if status[criterion] else 'FAIL'}")
        for detail in details:
            print(detail, file=sys.stderr)
        return 0 if all(status.values()) and unit_ok else 1

    if run_case("happy path", happy_path, details):
        for criterion in (
            "AC-HP-01",
            "AC-HP-02",
            "AC-HP-03",
            "AC-HP-04",
            "AC-HP-05",
        ):
            status[criterion] = True
    if run_case("helper failure fallback", lambda: fallback_path("false", None), details):
        status["AC-SP-01"] = True
    if run_case(
        "helper timeout fallback",
        lambda: fallback_path("sleep 3", 100),
        details,
    ):
        status["AC-SP-02"] = True
    if run_case("synthetic scopes", synthetic_scopes, details):
        status["AC-SP-03"] = True
    if run_case("clean protocol failure", clean_protocol_failure, details):
        status["AC-SP-04"] = True

    for criterion in CRITERIA:
        print(f"{criterion} {'PASS' if status[criterion] else 'FAIL'}")
    for detail in details:
        print(detail, file=sys.stderr)
    return 0 if all(status.values()) and unit_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
