"""Run the Rust-outer stabilization acceptance contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any, Callable

from .dap_support import DapClient, DapError, ROOT


RUST_FIXTURE = ROOT / "research" / "fixtures" / "rust_outer"
_configured_target = os.environ.get("CARGO_TARGET_DIR")
CARGO_TARGET_DIR = (
    Path(_configured_target)
    if _configured_target
    else RUST_FIXTURE / "target"
)
if not CARGO_TARGET_DIR.is_absolute():
    CARGO_TARGET_DIR = ROOT / CARGO_TARGET_DIR
RUST_BINARY = CARGO_TARGET_DIR / "debug" / "rust-outer-python-inner"
RUST_SOURCE = RUST_FIXTURE / "src" / "main.rs"
PYTHON_SOURCE = RUST_FIXTURE / "src" / "embedded.py"
CRITERIA = (
    "AC-BF-01",
    "AC-BF-02",
    "AC-BF-03",
    "AC-BF-04",
    "AC-BF-05",
    "AC-RP-01",
    "AC-RP-02",
    "AC-RP-03",
    "AC-RP-04",
    "AC-RP-05",
    "AC-RP-06",
    "AC-RP-07",
)
REQUIRED_NAMES = ["rust_callback", "python_inner", "python_outer", "rust_outer", "main"]


def proxy_command() -> list[str]:
    configured = os.environ.get("PYRUST_DAP_PROXY")
    if configured:
        return shlex.split(configured)
    return [
        str(ROOT / ".venv" / "bin" / "python"),
        str(ROOT / "prototype" / "adapter" / "__main__.py"),
    ]


def initialize(client: DapClient) -> None:
    request = client.send(
        "initialize",
        {
            "clientID": "pyrust-reverse-acceptance",
            "adapterID": "pyrust",
            "pathFormat": "path",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "supportsRunInTerminalRequest": False,
        },
    )
    client.response(request, timeout=10)


def python_libdir() -> str:
    return subprocess.check_output(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            "-c",
            "import sysconfig; print(sysconfig.get_config_var('LIBDIR'))",
        ],
        cwd=ROOT,
        text=True,
        timeout=5,
    ).strip()


def launch_arguments(
    *,
    helper_command: str | None = None,
    helper_timeout_ms: int | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "program": str(RUST_BINARY),
        "args": [],
        "cwd": str(ROOT),
        "env": {"LD_LIBRARY_PATH": python_libdir()},
        "terminal": "console",
        "consoleMode": "evaluate",
        "sourceLanguages": ["rust"],
        "pyrustPythonDebug": False,
    }
    if helper_command is not None:
        arguments["pyrustHelperCommand"] = helper_command
    if helper_timeout_ms is not None:
        arguments["pyrustHelperTimeoutMs"] = helper_timeout_ms
    return arguments


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
    client.event("initialized", timeout=10)
    breakpoint = client.send(
        "setBreakpoints",
        {
            "source": {"path": str(RUST_SOURCE)},
            "breakpoints": [{"line": 9}],
            "sourceModified": False,
        },
    )
    client.response(breakpoint, timeout=10)
    configuration = client.send("configurationDone")
    client.response(configuration, timeout=10)
    client.response(launch, timeout=15)
    stopped = client.event("stopped", timeout=20)
    return client, stopped


def stack(client: DapClient, thread_id: int, *, start: int = 0, levels: int = 80) -> dict[str, Any]:
    request = client.send(
        "stackTrace",
        {"threadId": thread_id, "startFrame": start, "levels": levels},
    )
    return client.response(request, timeout=15)


def frame_name(frame: dict[str, Any]) -> str:
    return str(frame.get("name", "")).rsplit("::", 1)[-1]


def user_frames(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        frame
        for frame in response.get("body", {}).get("stackFrames", [])
        if frame_name(frame) in set(REQUIRED_NAMES) | {"pyrust_embedded::python_inner"}
    ]


def frame_by_name(frames: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for frame in frames:
        if frame_name(frame) == name:
            return frame
    raise DapError(f"missing required frame {name!r}: {[frame_name(f) for f in frames]}")


def assert_source(frame: dict[str, Any], path: Path, line: int) -> None:
    actual = (frame.get("source") or {}).get("path", "")
    if actual != str(path) and not str(actual).endswith(str(path.relative_to(ROOT))):
        raise DapError(f"{frame_name(frame)} has unexpected source {actual!r}")
    if frame.get("line") != line:
        raise DapError(
            f"{frame_name(frame)} has line {frame.get('line')}, expected {line}"
        )


def callback_stop() -> tuple[DapClient, dict[str, Any]]:
    client, stopped = start_fixture()
    if "console is in 'evaluation' mode" not in output_text(client).lower():
        client.close()
        raise DapError("CodeLLDB did not start in evaluation mode")
    thread_id = stopped.get("body", {}).get("threadId")
    if not isinstance(thread_id, int) or thread_id <= 0:
        client.close()
        raise DapError(f"stopped event has no usable thread ID: {stopped}")
    if stopped.get("body", {}).get("reason") != "breakpoint":
        client.close()
        raise DapError(f"callback stop was not a breakpoint stop: {stopped}")
    return client, stopped


def reverse_happy_path() -> dict[str, Any]:
    client, stopped = callback_stop()
    try:
        thread_id = stopped.get("body", {}).get("threadId")
        if not isinstance(thread_id, int) or thread_id <= 0:
            raise DapError(f"stopped event has no usable thread ID: {stopped}")
        response = stack(client, thread_id)
        frames = user_frames(response)
        names = [frame_name(frame) for frame in frames]
        cursor = 0
        for name in REQUIRED_NAMES:
            try:
                cursor = names.index(name, cursor) + 1
            except ValueError as error:
                raise DapError(f"required reverse order missing from {names}") from error
        if response.get("body", {}).get("totalFrames", 0) < len(frames):
            raise DapError("stackTrace totalFrames is smaller than returned frames")
        return {"client": client, "thread_id": thread_id, "frames": frames}
    except Exception:
        client.close()
        raise


def rp_launch_and_stack() -> None:
    client, _ = callback_stop()
    client.close()


def rp_mixed_stack() -> None:
    result = reverse_happy_path()
    result["client"].close()


def rp_source_navigation() -> None:
    result = reverse_happy_path()
    try:
        frames = result["frames"]
        assert_source(frame_by_name(frames, "rust_callback"), RUST_SOURCE, 9)
        assert_source(frame_by_name(frames, "rust_outer"), RUST_SOURCE, 17)
        assert_source(frame_by_name(frames, "main"), RUST_SOURCE, 36)
        assert_source(frame_by_name(frames, "python_inner"), PYTHON_SOURCE, 4)
        assert_source(frame_by_name(frames, "python_outer"), PYTHON_SOURCE, 11)
    finally:
        result["client"].close()


def rp_native_identity() -> None:
    result = reverse_happy_path()
    try:
        frames = result["frames"]
        upper = frame_by_name(frames, "rust_callback")
        lower = frame_by_name(frames, "rust_outer")
        for frame in (upper, lower):
            frame_id = frame.get("id")
            if not isinstance(frame_id, int):
                raise DapError(f"{frame_name(frame)} has no native frame ID")
            request = result["client"].send("scopes", {"frameId": frame_id})
            response = result["client"].response(request, timeout=10)
            if not isinstance(response.get("body", {}).get("scopes"), list):
                raise DapError(f"scopes failed for native frame {frame_id}")
    finally:
        result["client"].close()


def rp_synthetic_behavior() -> None:
    result = reverse_happy_path()
    try:
        client = result["client"]
        python_frame = frame_by_name(result["frames"], "python_inner")
        frame_id = python_frame.get("id")
        if not isinstance(frame_id, int):
            raise DapError("synthetic Python frame has no ID")
        scopes = client.send("scopes", {"frameId": frame_id})
        scope_list = client.response(scopes, timeout=10).get("body", {}).get("scopes")
        if not isinstance(scope_list, list) or len(scope_list) != 1:
            raise DapError(f"synthetic Python scopes were malformed: {scope_list}")
        reference = scope_list[0].get("variablesReference")
        if reference != frame_id:
            raise DapError(f"synthetic Python scope reference was malformed: {scope_list}")
        variables = client.send("variables", {"variablesReference": reference})
        variable_list = client.response(variables, timeout=10).get("body", {}).get(
            "variables"
        )
        if not isinstance(variable_list, list) or not any(
            variable.get("name") == "value" and variable.get("value") == "20"
            for variable in variable_list
        ):
            raise DapError(f"synthetic Python local value was not returned: {variable_list}")
        evaluate = client.send(
            "evaluate",
            {"expression": "value + 1", "frameId": frame_id, "context": "watch"},
        )
        response = client.response(evaluate, timeout=10)
        if response.get("body", {}).get("result") != "21":
            raise DapError(f"synthetic Python evaluation did not return 21: {response}")
        unsafe_evaluate = client.send(
            "evaluate",
            {
                "expression": "__import__('os')",
                "frameId": frame_id,
                "context": "watch",
            },
        )
        unsafe_response = client.wait_for(
            lambda message: message.get("type") == "response"
            and message.get("request_seq") == unsafe_evaluate,
            timeout=10,
        )
        if unsafe_response.get("success", True):
            raise DapError("unsafe synthetic Python evaluation was accepted")
        if "Call" not in str(unsafe_response.get("message", "")):
            raise DapError(
                f"unsafe Python evaluation had an unclear failure: {unsafe_response}"
            )
    finally:
        result["client"].close()


def rp_repeated_stop() -> None:
    client, stopped = start_fixture()
    try:
        thread_id = stopped["body"]["threadId"]
        first = user_frames(stack(client, thread_id))
        first_python_ids = {
            frame["id"] for frame in first if frame_name(frame).startswith("python_")
        }
        if not first_python_ids:
            raise DapError("first callback stop had no synthetic Python frames")
        continue_request = client.send(
            "continue", {"threadId": thread_id, "singleThread": True}
        )
        client.response(continue_request, timeout=10)
        client.event("stopped", timeout=20)
        second = user_frames(stack(client, thread_id))
        second_python_ids = {
            frame["id"] for frame in second if frame_name(frame).startswith("python_")
        }
        if not second_python_ids:
            raise DapError("second callback stop had no synthetic Python frames")
        if first_python_ids & second_python_ids:
            raise DapError("synthetic Python IDs were reused across stop epochs")
        stale = client.send("scopes", {"frameId": next(iter(first_python_ids))})
        response = client.wait_for(
            lambda message: message.get("type") == "response"
            and message.get("request_seq") == stale,
            timeout=10,
        )
        if response.get("success", True):
            raise DapError("stale synthetic frame ID was accepted")
    finally:
        client.close()


def helper_command(
    frames: list[dict[str, Any]],
    *,
    match_thread: bool = True,
) -> str:
    payload = json.dumps({"threads": [{"threadId": 0, "frames": frames}]})
    code = (
        "import json,os; p=json.loads("
        + repr(payload)
    )
    if match_thread:
        # The proxy's fixed single-thread fallback maps the target PID to its
        # DAP thread ID; replacing zero keeps this command deterministic.
        code += "); p['threads'][0]['threadId']=int(os.environ['PYRUST_TARGET_PID']); "
    else:
        code += "); "
    code += "print(json.dumps(p))"
    # MixedStackHooks expands {pid} in helper commands before execution.
    # Protect the JSON object braces from that formatting pass.
    code = code.replace("{", "{{").replace("}", "}}")
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def fallback_case(
    *,
    command: str,
    timeout_ms: int | None = None,
    expect_timeout: bool = False,
) -> None:
    client, stopped = start_fixture(
        helper_command=command,
        helper_timeout_ms=timeout_ms,
    )
    try:
        started = time.monotonic()
        response = stack(client, stopped["body"]["threadId"])
        elapsed = time.monotonic() - started
        frames = user_frames(response)
        if any(frame_name(frame).startswith("python_") for frame in frames):
            raise DapError("fallback returned synthetic Python frames")
        if elapsed > 2:
            raise DapError(f"fallback took {elapsed:.2f}s")
        if not frames:
            raise DapError("fallback returned no native frames")
        if not all(
            isinstance(frame.get("id"), int)
            and frame.get("instructionPointerReference")
            for frame in frames
        ):
            raise DapError("fallback did not preserve native frame identity")
        if expect_timeout and "timeout" not in output_text(client).lower():
            raise DapError("timeout fallback emitted no timeout diagnostic")
        threads = client.send("threads")
        if not client.response(threads, timeout=10).get("body", {}).get("threads"):
            raise DapError("session was unusable after fallback")
    finally:
        client.close()


def output_text(client: DapClient) -> str:
    return "\n".join(
        message.get("body", {}).get("output", "")
        for message in client.messages
        if message.get("type") == "event" and message.get("event") == "output"
    )


def rp_fallbacks() -> None:
    valid_frames = [
        {"name": "python_inner", "path": str(PYTHON_SOURCE), "line": 2},
        {"name": "python_outer", "path": str(PYTHON_SOURCE), "line": 6},
    ]
    fallback_case(command="false")
    fallback_case(
        command=f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(3)')}",
        timeout_ms=100,
        expect_timeout=True,
    )
    fallback_case(command=helper_command(valid_frames, match_thread=False))


def run_unittest(name: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "unittest", name],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(["prototype/python", "."])},
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise DapError(result.stdout + result.stderr)


def run_case(
    label: str,
    action: Callable[[], Any],
    details: list[str],
) -> bool:
    try:
        action()
        return True
    except Exception as error:
        details.append(f"{label}: {error}")
        return False


def main() -> int:
    details: list[str] = []
    status = {criterion: False for criterion in CRITERIA}
    unit_cases = {
        "AC-BF-01": "tests.acceptance.test_reverse_contract.ReverseStabilizationContractTests.test_ac_bf_01_single_worker_for_concurrent_blocked_requests",
        "AC-BF-02": "tests.acceptance.test_reverse_contract.ReverseStabilizationContractTests.test_ac_bf_02_timeout_opens_circuit_and_later_requests_are_immediate",
        "AC-BF-03": "tests.acceptance.test_reverse_contract.ReverseStabilizationContractTests.test_ac_bf_03_timeout_diagnostic_is_emitted_once_across_epochs",
        "AC-BF-05": "tests.acceptance.test_reverse_contract.ReverseStabilizationContractTests.test_ac_bf_05_late_result_cannot_allocate_into_new_epoch",
        "AC-RP-02": "tests.acceptance.test_reverse_contract.ReverseShapeContractTests.test_ac_rp_02_required_reverse_order_and_post_merge_paging",
        "AC-RP-05": "tests.acceptance.test_reverse_contract.ReverseShapeContractTests.test_ac_rp_05_synthetic_frames_expose_snapshot_locals",
        "AC-RP-06": "tests.acceptance.test_reverse_contract.ReverseShapeContractTests.test_ac_rp_06_unknown_boundaries_preserve_native_stack",
    }
    for criterion, test_name in unit_cases.items():
        status[criterion] = run_case(
            f"{criterion} contract",
            lambda test_name=test_name: run_unittest(test_name),
            details,
        )

    try:
        first_slice = subprocess.run(
            [str(ROOT / "scripts" / "accept-first-slice.sh")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=330,
        )
        status["AC-BF-04"] = first_slice.returncode == 0
        if not status["AC-BF-04"]:
            details.append(
                "AC-BF-04 existing slice failed:\n"
                + first_slice.stdout
                + first_slice.stderr
            )
    except subprocess.TimeoutExpired as error:
        details.append(f"AC-BF-04 existing slice timed out: {error}")

    integration_cases: tuple[tuple[str, Callable[[], Any]], ...] = (
        ("AC-RP-01", rp_launch_and_stack),
        ("AC-RP-02", rp_mixed_stack),
        ("AC-RP-03", rp_source_navigation),
        ("AC-RP-04", rp_native_identity),
        ("AC-RP-05", rp_synthetic_behavior),
        ("AC-RP-06", rp_fallbacks),
        ("AC-RP-07", rp_repeated_stop),
    )
    for criterion, action in integration_cases:
        status[criterion] = run_case(criterion, action, details)

    for criterion in CRITERIA:
        print(f"{criterion} {'PASS' if status[criterion] else 'FAIL'}")
    for detail in details:
        print(detail, file=sys.stderr)
    return 0 if all(status.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
