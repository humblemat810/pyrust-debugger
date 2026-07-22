"""Black-box acceptance for Python-owned debugpy stops in a PyRust session."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import Any

from .dap_support import (
    DapClient,
    DapError,
    PYTHON,
    PYTHON_SOURCE,
    ROOT,
    RUST_SOURCE,
    proxy_command,
)
from .run_acceptance import initialize


FIXTURE_DRIVER = ROOT / "tests" / "acceptance" / "fixture_driver.py"
THREADED_DRIVER = ROOT / "tests" / "acceptance" / "threaded_fixture_driver.py"
MULTIPROCESS_DRIVER = (
    ROOT / "tests" / "acceptance" / "multiprocess_fixture_driver.py"
)
MULTIPROCESS_WORKER = ROOT / "tests" / "acceptance" / "multiprocess_worker.py"
DYNAMIC_CALL_DRIVER = (
    ROOT / "tests" / "acceptance" / "dynamic_call_fixture_driver.py"
)
ARBITRARY_BOUNDARY_DRIVER = (
    ROOT / "tests" / "acceptance" / "arbitrary_boundary_fixture_driver.py"
)
RUST_OUTER_FIXTURE = ROOT / "research" / "fixtures" / "rust_outer"
RUST_OUTER_TARGET = Path(
    os.environ.get("CARGO_TARGET_DIR", str(RUST_OUTER_FIXTURE / "target"))
)
RUST_OUTER_BINARY = RUST_OUTER_TARGET / "debug" / "rust-outer-python-inner"
RUST_OUTER_SOURCE = RUST_OUTER_FIXTURE / "src" / "main.rs"
EMBEDDED_PYTHON_SOURCE = RUST_OUTER_FIXTURE / "src" / "embedded.py"
RUST_THREADED_BINARY = (
    RUST_OUTER_TARGET / "debug" / "rust-outer-python-threads"
)
RUST_THREADED_SOURCE = RUST_OUTER_FIXTURE / "src" / "threaded_main.rs"
RUST_THREADED_PYTHON_SOURCE = (
    RUST_OUTER_FIXTURE / "src" / "threaded_embedded.py"
)
ASYNC_DRIVER = ROOT / "tests" / "acceptance" / "async_fixture_driver.py"
RUST_ASYNC_BINARY = RUST_OUTER_TARGET / "debug" / "rust-outer-python-async"
RUST_ASYNC_SOURCE = RUST_OUTER_FIXTURE / "src" / "async_main.rs"
RUST_ASYNC_PYTHON_SOURCE = RUST_OUTER_FIXTURE / "src" / "async_embedded.py"

CRITERIA = (
    "AC-DP-01",
    "AC-DP-02",
    "AC-DP-03",
    "AC-DP-04",
    "AC-DP-05",
    "AC-DP-06",
    "AC-DP-07",
    "AC-DP-08",
    "AC-DP-09",
    "AC-DP-10",
    "AC-DP-11",
    "AC-DP-12",
    "AC-DP-13",
    "AC-DP-14",
    "AC-DP-15",
    "AC-DP-16",
    "AC-DP-17",
    "AC-DP-18",
    "AC-DP-19",
    "AC-DP-20",
    "AC-DP-21",
    "AC-DP-22",
    "AC-DP-23",
    "AC-DP-24",
    "AC-DP-25",
    "AC-DP-26",
    "AC-DP-27",
    "AC-DP-28",
)


def _launch(
    client: DapClient,
    *,
    program: Path,
    args: list[str],
    env: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> int:
    arguments: dict[str, Any] = {
        "program": str(program),
        "args": args,
        "cwd": str(ROOT),
        "terminal": "console",
        "consoleMode": "evaluate",
        "sourceLanguages": ["rust"],
        "pyrustPythonDebug": True,
    }
    if env:
        arguments["env"] = env
    if extra:
        arguments.update(extra)
    return client.send("launch", arguments)


def _set_breakpoint(client: DapClient, source: Path, line: int) -> None:
    request = client.send(
        "setBreakpoints",
        {
            "source": {"path": str(source)},
            "breakpoints": [{"line": line}],
            "sourceModified": False,
        },
    )
    response = client.response(request, timeout=10)
    breakpoints = response.get("body", {}).get("breakpoints")
    if not isinstance(breakpoints, list) or len(breakpoints) != 1:
        raise DapError(f"debugpy breakpoint response was malformed: {response}")


def _stack(client: DapClient, thread_id: int) -> list[dict[str, Any]]:
    request = client.send("stackTrace", {"threadId": thread_id, "levels": 80})
    frames = client.response(request, timeout=15).get("body", {}).get("stackFrames")
    if not isinstance(frames, list):
        raise DapError(f"stackTrace response was malformed: {frames}")
    return frames


def _evaluate(
    client: DapClient,
    frame_id: int,
    expression: str,
    *,
    context: str = "watch",
) -> str:
    request = client.send(
        "evaluate",
        {"expression": expression, "frameId": frame_id, "context": context},
    )
    return str(client.response(request, timeout=10).get("body", {}).get("result"))


def _python_libdir() -> str:
    return subprocess.check_output(
        [
            str(PYTHON),
            "-c",
            "import sysconfig; print(sysconfig.get_config_var('LIBDIR'))",
        ],
        text=True,
        timeout=5,
    ).strip()


def _python_outer_stop() -> tuple[DapClient, dict[str, Any]]:
    client = DapClient(proxy_command())
    initialize(client)
    launch = _launch(client, program=PYTHON, args=[str(FIXTURE_DRIVER)])
    client.event("initialized", timeout=10)
    _set_breakpoint(client, PYTHON_SOURCE, 10)
    _set_breakpoint(client, RUST_SOURCE, 6)
    configured = client.send("configurationDone")
    client.response(configured, timeout=10)
    client.response(launch, timeout=20)
    return client, client.event("stopped", timeout=30)


def full_python_evaluation() -> tuple[DapClient, int]:
    client, stopped = _python_outer_stop()
    try:
        thread_id = stopped.get("body", {}).get("threadId")
        if not isinstance(thread_id, int):
            raise DapError(f"debugpy stop has no thread ID: {stopped}")
        frame = _stack(client, thread_id)[0]
        frame_id = frame.get("id")
        if not isinstance(frame_id, int) or frame.get("name") != "python_outer":
            raise DapError(f"debugpy did not stop in python_outer: {frame}")
        if _evaluate(client, frame_id, "type(2).__name__") != "'int'":
            raise DapError("debugpy did not evaluate a Python function call")
        if _evaluate(client, frame_id, "import sys", context="repl") != "":
            raise DapError("debugpy did not accept a Python import statement")
        if _evaluate(client, frame_id, "sys.version_info[:2]", context="repl") != "(3, 14)":
            raise DapError("debugpy did not preserve imported Python names")
        if _evaluate(client, frame_id, "__import__('sys').version_info[:2]") != "(3, 14)":
            raise DapError("debugpy did not evaluate a Python import expression")
        return client, thread_id
    except Exception:
        client.close()
        raise


def python_stop_and_native_handoff() -> None:
    client, python_thread = full_python_evaluation()
    try:
        continued = client.send("continue", {"threadId": python_thread})
        client.response(continued, timeout=10)
        native_stop = client.event("stopped", timeout=30)
        native_thread = native_stop.get("body", {}).get("threadId")
        if not isinstance(native_thread, int):
            raise DapError(f"native stop has no thread ID: {native_stop}")
        frames = _stack(client, native_thread)
        names = [str(frame.get("name", "")).rsplit("::", 1)[-1] for frame in frames]
        if names[:4] != ["rust_inner", "rust_outer", "python_inner", "python_outer"]:
            raise DapError(f"native handoff lost mixed stack: {names[:8]}")
        native_id = frames[0].get("id")
        if not isinstance(native_id, int) or _evaluate(client, native_id, "value") != "20":
            raise DapError("native handoff did not restore Rust evaluation")
    finally:
        client.close()


def python_step_in() -> None:
    client, stopped = _python_outer_stop()
    try:
        thread_id = stopped.get("body", {}).get("threadId")
        if not isinstance(thread_id, int):
            raise DapError(f"Python step stop has no thread ID: {stopped}")
        for expected_name, expected_line in (
            ("python_outer", 11),
            ("python_inner", 5),
        ):
            stepped = client.send(
                "stepIn",
                {"threadId": thread_id, "granularity": "line"},
            )
            client.response(stepped, timeout=10)
            stopped = client.event("stopped", timeout=30)
            thread_id = stopped.get("body", {}).get("threadId")
            if not isinstance(thread_id, int):
                raise DapError(f"Python step result has no thread ID: {stopped}")
            frame = _stack(client, thread_id)[0]
            if (
                frame.get("name") != expected_name
                or frame.get("line") != expected_line
                or (frame.get("source") or {}).get("path") != str(PYTHON_SOURCE)
            ):
                raise DapError(f"debugpy step did not reach Python frame: {frame}")
    finally:
        client.close()


def python_threads() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(client, program=PYTHON, args=[str(THREADED_DRIVER)])
        client.event("initialized", timeout=10)
        _set_breakpoint(client, THREADED_DRIVER, 24)
        configured = client.send("configurationDone")
        client.response(configured, timeout=10)
        client.response(launch, timeout=20)
        stopped = client.event("stopped", timeout=30)
        thread_id = stopped.get("body", {}).get("threadId")
        if not isinstance(thread_id, int):
            raise DapError(f"thread stop has no thread ID: {stopped}")
        threads = client.response(client.send("threads"), timeout=10).get("body", {}).get(
            "threads"
        )
        if not isinstance(threads, list) or len(threads) < 3:
            raise DapError(f"debugpy did not expose the main and worker threads: {threads}")
        frame_id = _stack(client, thread_id)[0].get("id")
        if not isinstance(frame_id, int):
            raise DapError("thread Python frame has no virtual ID")
        result = _evaluate(
            client,
            frame_id,
            "worker_label + ':' + str(worker_value)",
        )
        if result not in {"'worker-A:20'", "'worker-B:40'"}:
            raise DapError(f"thread evaluation was not thread-local: {result!r}")
    finally:
        client.close()


def python_processes() -> None:
    with TemporaryDirectory(prefix="pyrust-debugpy-processes-") as directory:
        registry = Path(directory)
        client = DapClient(proxy_command())
        try:
            initialize(client)
            launch = _launch(
                client,
                program=PYTHON,
                args=[str(MULTIPROCESS_DRIVER)],
                env={"PYRUST_CHILD_REGISTRY": str(registry)},
                extra={
                    "pyrustChildRegistryPath": str(registry),
                    "pyrustProcessMode": "children",
                },
            )
            client.event("initialized", timeout=10)
            _set_breakpoint(client, MULTIPROCESS_WORKER, 50)
            _set_breakpoint(client, RUST_SOURCE, 6)
            configured = client.send("configurationDone")
            client.response(configured, timeout=10)
            client.response(launch, timeout=20)
            stopped = client.event("stopped", timeout=45)
            process_id = stopped.get("body", {}).get("systemProcessId")
            thread_id = stopped.get("body", {}).get("threadId")
            if not isinstance(process_id, int) or not isinstance(thread_id, int):
                raise DapError(f"child debugpy stop had invalid identity: {stopped}")
            tree = client.response(
                client.send("pyrust/processTree"),
                timeout=10,
            ).get("body", {}).get("processes")
            process = next(
                (
                    item
                    for item in tree
                    if isinstance(item, dict) and item.get("processId") == process_id
                ),
                None,
            ) if isinstance(tree, list) else None
            process_threads = process.get("threads") if isinstance(process, dict) else None
            if (
                not isinstance(process_threads, list)
                or thread_id
                not in {
                    item.get("threadId")
                    for item in process_threads
                    if isinstance(item, dict)
                }
                or len(process_threads)
                != len(
                    {
                        item.get("threadId")
                        for item in process_threads
                        if isinstance(item, dict)
                    }
                )
            ):
                raise DapError(
                    f"process tree did not expose one unique stopped Python thread: "
                    f"{process}"
                )
            worker = _stack(client, thread_id)[0]
            frame_id = worker.get("id")
            if (
                not isinstance(frame_id, int)
                or worker.get("name") != "python_worker"
                or (worker.get("source") or {}).get("path") != str(MULTIPROCESS_WORKER)
            ):
                raise DapError(f"child debugpy frame was not the worker: {worker}")
            result = _evaluate(
                client,
                frame_id,
                "(__import__('os').getpid(), label, value)",
            )
            if str(process_id) not in result or "process-" not in result:
                raise DapError(
                    f"child evaluation leaked process identity: {process_id}, {result}"
                )
        finally:
            client.close()


def rust_outer_python_stop() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=RUST_OUTER_BINARY,
            args=[],
            env={"LD_LIBRARY_PATH": _python_libdir()},
        )
        client.event("initialized", timeout=10)
        _set_breakpoint(client, EMBEDDED_PYTHON_SOURCE, 4)
        _set_breakpoint(client, RUST_OUTER_SOURCE, 8)
        configured = client.send("configurationDone")
        client.response(configured, timeout=10)
        client.response(launch, timeout=20)
        python_stop = client.event("stopped", timeout=30)
        python_thread = python_stop.get("body", {}).get("threadId")
        if not isinstance(python_thread, int):
            raise DapError(f"reverse Python stop has no thread ID: {python_stop}")
        python_frames = _stack(client, python_thread)
        python_frame = python_frames[0]
        python_id = python_frame.get("id")
        if (
            python_frame.get("name") != "python_inner"
            or (python_frame.get("source") or {}).get("path")
            != str(EMBEDDED_PYTHON_SOURCE)
            or not isinstance(python_id, int)
        ):
            raise DapError(f"reverse debugpy frame was malformed: {python_frame}")
        if _evaluate(client, python_id, "__import__('sys').version_info[:2]") != "(3, 14)":
            raise DapError("reverse debugpy evaluation did not support imports")

        continued = client.send("continue", {"threadId": python_thread})
        client.response(continued, timeout=10)
        native_stop = client.event("stopped", timeout=30)
        native_thread = native_stop.get("body", {}).get("threadId")
        if not isinstance(native_thread, int):
            raise DapError(f"reverse native stop has no thread ID: {native_stop}")
        names = [
            str(frame.get("name", "")).rsplit("::", 1)[-1]
            for frame in _stack(client, native_thread)
        ]
        if names[:3] != ["rust_callback", "python_inner", "python_outer"]:
            raise DapError(f"reverse native handoff lost mixed stack: {names[:8]}")
    finally:
        client.close()


def rust_outer_restart() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=RUST_OUTER_BINARY,
            args=[],
            env={"LD_LIBRARY_PATH": _python_libdir()},
        )
        client.event("initialized", timeout=10)
        _set_breakpoint(client, EMBEDDED_PYTHON_SOURCE, 4)
        _set_breakpoint(client, RUST_OUTER_SOURCE, 8)
        configured = client.send("configurationDone")
        client.response(configured, timeout=10)
        client.response(launch, timeout=20)

        for restart in (False, True):
            if restart:
                client.response(client.send("restart", {}), timeout=30)
            stopped = client.event("stopped", timeout=30)
            thread_id = stopped.get("body", {}).get("threadId")
            if not isinstance(thread_id, int):
                raise DapError(f"restart Python stop has no thread ID: {stopped}")
            frame = _stack(client, thread_id)[0]
            frame_id = frame.get("id")
            if (
                frame.get("name") != "python_inner"
                or not isinstance(frame_id, int)
                or _evaluate(client, frame_id, "value + 1") != "21"
            ):
                raise DapError(
                    f"Rust-outer restart did not restore live debugpy: {frame}"
                )
    finally:
        client.close()


def rust_outer_cross_language_step_in() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=RUST_OUTER_BINARY,
            args=[],
            env={"LD_LIBRARY_PATH": _python_libdir()},
        )
        client.event("initialized", timeout=10)
        _set_breakpoint(client, EMBEDDED_PYTHON_SOURCE, 4)
        client.response(client.send("configurationDone"), timeout=10)
        client.response(launch, timeout=20)

        python_stop = client.event("stopped", timeout=30)
        python_thread = python_stop.get("body", {}).get("threadId")
        if not isinstance(python_thread, int):
            raise DapError(f"cross-step Python stop has no thread ID: {python_stop}")
        client.response(
            client.send(
                "stepIn",
                {"threadId": python_thread, "granularity": "line"},
            ),
            timeout=10,
        )

        rust_stop = client.event("stopped", timeout=30)
        rust_thread = rust_stop.get("body", {}).get("threadId")
        if not isinstance(rust_thread, int):
            raise DapError(f"cross-step Rust stop has no thread ID: {rust_stop}")
        frame = _stack(client, rust_thread)[0]
        frame_id = frame.get("id")
        if (
            str(frame.get("name", "")).rsplit("::", 1)[-1] != "rust_callback"
            or not isinstance(frame_id, int)
            or _evaluate(client, frame_id, "1 + 1") != "2"
        ):
            raise DapError(f"Python-to-Rust step handoff failed: {frame}")
    finally:
        client.close()


def live_python_assignment() -> None:
    client, stopped = _python_outer_stop()
    try:
        thread_id = stopped.get("body", {}).get("threadId")
        if not isinstance(thread_id, int):
            raise DapError(f"Python assignment stop has no thread ID: {stopped}")
        frame = _stack(client, thread_id)[0]
        frame_id = frame.get("id")
        if not isinstance(frame_id, int):
            raise DapError(f"Python assignment frame has no ID: {frame}")
        scopes = client.response(
            client.send("scopes", {"frameId": frame_id}),
            timeout=10,
        ).get("body", {}).get("scopes")
        if not isinstance(scopes, list) or not scopes:
            raise DapError(f"Python assignment scopes were malformed: {scopes}")
        reference = scopes[0].get("variablesReference")
        if not isinstance(reference, int) or reference <= 0:
            raise DapError(f"Python locals scope has no reference: {scopes[0]}")
        response = client.response(
            client.send(
                "setVariable",
                {
                    "variablesReference": reference,
                    "name": "value",
                    "value": "41",
                },
            ),
            timeout=10,
        )
        if response.get("body", {}).get("value") != "41":
            raise DapError(f"debugpy did not assign the Python local: {response}")
        if _evaluate(client, frame_id, "value") != "41":
            raise DapError("the assigned Python local was not visible to evaluation")
    finally:
        client.close()


def live_rust_assignment() -> None:
    client, python_thread = full_python_evaluation()
    try:
        client.response(
            client.send("continue", {"threadId": python_thread}),
            timeout=10,
        )
        stopped = client.event("stopped", timeout=30)
        thread_id = stopped.get("body", {}).get("threadId")
        if not isinstance(thread_id, int):
            raise DapError(f"Rust assignment stop has no thread ID: {stopped}")
        frame = _stack(client, thread_id)[0]
        frame_id = frame.get("id")
        if not isinstance(frame_id, int):
            raise DapError(f"Rust assignment frame has no ID: {frame}")
        scopes = client.response(
            client.send("scopes", {"frameId": frame_id}),
            timeout=10,
        ).get("body", {}).get("scopes")
        local_scope = next(
            (
                scope
                for scope in scopes
                if isinstance(scope, dict) and scope.get("name") == "Local"
            ),
            None,
        ) if isinstance(scopes, list) else None
        reference = (
            local_scope.get("variablesReference")
            if isinstance(local_scope, dict)
            else None
        )
        if not isinstance(reference, int) or reference <= 0:
            raise DapError(f"Rust local scope has no reference: {scopes}")
        response = client.response(
            client.send(
                "setVariable",
                {
                    "variablesReference": reference,
                    "name": "value",
                    "value": "41",
                },
            ),
            timeout=10,
        )
        if response.get("body", {}).get("value") != "41":
            raise DapError(f"CodeLLDB did not assign the Rust local: {response}")
        if _evaluate(client, frame_id, "value") != "41":
            raise DapError("the assigned Rust local was not visible to evaluation")
    finally:
        client.close()


def rust_stop_to_live_debugpy_frame() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=RUST_OUTER_BINARY,
            args=[],
            env={"LD_LIBRARY_PATH": _python_libdir()},
        )
        client.event("initialized", timeout=10)
        _set_breakpoint(client, RUST_OUTER_SOURCE, 8)
        client.response(client.send("configurationDone"), timeout=10)
        client.response(launch, timeout=20)
        stopped = client.event("stopped", timeout=30)
        frames = _stack(client, stopped["body"]["threadId"])
        python_frame = next(
            (frame for frame in frames if frame.get("name") == "python_inner"),
            None,
        )
        if not isinstance(python_frame, dict):
            raise DapError(f"Rust stop had no Python frame: {frames[:8]}")
        client.response(
            client.send("scopes", {"frameId": python_frame["id"]}),
            timeout=10,
        )
        python_stop = client.event("stopped", timeout=30)
        python_thread = python_stop.get("body", {}).get("threadId")
        if not isinstance(python_thread, int):
            raise DapError(f"debugpy handoff had no thread: {python_stop}")
        live = _stack(client, python_thread)[0]
        frame_id = live.get("id")
        if (
            live.get("name") != "python_inner"
            or not isinstance(frame_id, int)
            or _evaluate(client, frame_id, "__import__('sys').version_info[:2]")
            != "(3, 14)"
        ):
            raise DapError(f"Rust stop did not hand off to live debugpy: {live}")
        scopes = client.response(
            client.send("scopes", {"frameId": frame_id}),
            timeout=10,
        ).get("body", {}).get("scopes")
        local = next(
            (
                scope
                for scope in scopes
                if isinstance(scope, dict) and scope.get("name") == "Locals"
            ),
            None,
        ) if isinstance(scopes, list) else None
        reference = local.get("variablesReference") if isinstance(local, dict) else None
        if not isinstance(reference, int):
            raise DapError(f"live debugpy handoff had no locals: {scopes}")
        client.response(
            client.send(
                "setVariable",
                {
                    "variablesReference": reference,
                    "name": "value",
                    "value": "41",
                },
            ),
            timeout=10,
        )
        if _evaluate(client, frame_id, "value") != "41":
            raise DapError("live debugpy handoff did not mutate the Python frame")
    finally:
        client.close()


def python_stop_to_live_codelldb_frame() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=RUST_OUTER_BINARY,
            args=[],
            env={"LD_LIBRARY_PATH": _python_libdir()},
        )
        client.event("initialized", timeout=10)
        _set_breakpoint(client, EMBEDDED_PYTHON_SOURCE, 4)
        client.response(client.send("configurationDone"), timeout=10)
        client.response(launch, timeout=20)
        stopped = client.event("stopped", timeout=30)
        thread_id = stopped.get("body", {}).get("threadId")
        if not isinstance(thread_id, int):
            raise DapError(f"Python stop had no thread: {stopped}")
        frames = _stack(client, thread_id)
        rust_frame = next(
            (
                frame
                for frame in frames
                if str(frame.get("name", "")).endswith("::rust_outer")
            ),
            None,
        )
        if not isinstance(rust_frame, dict):
            raise DapError(f"Python stop had no outer Rust frame: {frames}")
        rust_id = rust_frame.get("id")
        if not isinstance(rust_id, int) or _evaluate(client, rust_id, "1 + 1") != "2":
            raise DapError(f"outer Rust frame was not live CodeLLDB: {rust_frame}")
        rust_scopes = client.response(
            client.send("scopes", {"frameId": rust_id}),
            timeout=10,
        ).get("body", {}).get("scopes")
        local = next(
            (
                scope
                for scope in rust_scopes
                if isinstance(scope, dict) and scope.get("name") == "Local"
            ),
            None,
        ) if isinstance(rust_scopes, list) else None
        reference = local.get("variablesReference") if isinstance(local, dict) else None
        if not isinstance(reference, int):
            raise DapError(f"outer Rust frame had no local scope: {rust_scopes}")
        response = client.response(
            client.send(
                "setVariable",
                {
                    "variablesReference": reference,
                    "name": "outer_value",
                    "value": "41",
                },
            ),
            timeout=10,
        )
        if (
            response.get("body", {}).get("value") != "41"
            or _evaluate(client, rust_id, "outer_value") != "41"
        ):
            raise DapError(f"outer Rust mutation was not live: {response}")
        python_frame = frames[0]
        client.response(
            client.send("scopes", {"frameId": python_frame["id"]}),
            timeout=10,
        )
        if (
            _evaluate(
                client,
                python_frame["id"],
                "__import__('sys').version_info[:2]",
            )
            != "(3, 14)"
        ):
            raise DapError("returning from Rust frame did not restore debugpy")
    finally:
        client.close()


def child_rust_stop_to_live_debugpy_frame() -> None:
    with TemporaryDirectory(prefix="pyrust-debugpy-handoff-") as directory:
        client = DapClient(proxy_command())
        try:
            initialize(client)
            launch = _launch(
                client,
                program=PYTHON,
                args=[str(MULTIPROCESS_DRIVER)],
                env={"PYRUST_CHILD_REGISTRY": directory},
                extra={
                    "pyrustChildRegistryPath": directory,
                    "pyrustProcessMode": "children",
                },
            )
            client.event("initialized", timeout=10)
            _set_breakpoint(client, RUST_SOURCE, 6)
            client.response(client.send("configurationDone"), timeout=10)
            client.response(launch, timeout=20)
            stopped = client.event("stopped", timeout=45)
            process_id = stopped.get("body", {}).get("systemProcessId")
            thread_id = stopped.get("body", {}).get("threadId")
            if not isinstance(process_id, int) or not isinstance(thread_id, int):
                raise DapError(f"child Rust stop had no identity: {stopped}")
            frames = _stack(client, thread_id)
            python_frame = next(
                (frame for frame in frames if frame.get("name") == "python_worker"),
                None,
            )
            if not isinstance(python_frame, dict):
                raise DapError(f"child Rust stop had no Python frame: {frames[:10]}")
            client.response(
                client.send("scopes", {"frameId": python_frame["id"]}),
                timeout=15,
            )
            deadline = __import__("time").monotonic() + 40
            while True:
                remaining = deadline - __import__("time").monotonic()
                if remaining <= 0:
                    raise DapError("child debugpy handoff did not stop")
                python_stop = client.event("stopped", timeout=remaining)
                body = python_stop.get("body", {})
                if (
                    body.get("systemProcessId") == process_id
                    and isinstance(body.get("threadId"), int)
                    and body["threadId"] >= 1_600_000_000
                ):
                    break
            live = _stack(client, python_stop["body"]["threadId"])[0]
            frame_id = live.get("id")
            if (
                live.get("name") != "python_worker"
                or not isinstance(frame_id, int)
                or _evaluate(client, frame_id, "type(release).__name__")
                != "'PosixPath'"
                or _evaluate(client, frame_id, "__import__('sys').version_info[:2]")
                != "(3, 14)"
            ):
                raise DapError(f"child Python frame was not live debugpy: {live}")
        finally:
            client.close()


def child_user_breakpoint_preserves_debugpy_handoff() -> None:
    with TemporaryDirectory(prefix="pyrust-debugpy-user-breakpoint-") as directory:
        client = DapClient(proxy_command())
        try:
            initialize(client)
            launch = _launch(
                client,
                program=PYTHON,
                args=[str(MULTIPROCESS_DRIVER)],
                env={
                    "PYRUST_CHILD_REGISTRY": directory,
                    "PYRUST_CHILD_COUNT": "1",
                },
                extra={
                    "pyrustChildRegistryPath": directory,
                    "pyrustProcessMode": "children",
                },
            )
            client.event("initialized", timeout=10)
            _set_breakpoint(client, MULTIPROCESS_WORKER, 52)
            _set_breakpoint(client, RUST_SOURCE, 6)
            client.response(client.send("configurationDone"), timeout=10)
            client.response(launch, timeout=20)

            python_stop = client.event("stopped", timeout=45)
            process_id = python_stop.get("body", {}).get("systemProcessId")
            python_thread = python_stop.get("body", {}).get("threadId")
            if (
                not isinstance(process_id, int)
                or not isinstance(python_thread, int)
                or python_thread < 1_600_000_000
            ):
                raise DapError(
                    f"child user breakpoint was not owned by debugpy: {python_stop}"
                )
            python_frame = _stack(client, python_thread)[0]
            if (
                python_frame.get("name") != "python_worker"
                or python_frame.get("line") != 52
            ):
                raise DapError(
                    f"child user breakpoint stopped in the wrong frame: {python_frame}"
                )

            client.response(
                client.send("continue", {"threadId": python_thread}),
                timeout=10,
            )
            rust_stop = client.event("stopped", timeout=45)
            rust_thread = rust_stop.get("body", {}).get("threadId")
            if (
                rust_stop.get("body", {}).get("systemProcessId") != process_id
                or not isinstance(rust_thread, int)
                or rust_thread >= 1_600_000_000
            ):
                raise DapError(f"child did not hand off from debugpy to Rust: {rust_stop}")

            frames = _stack(client, rust_thread)
            snapshot = next(
                (
                    frame
                    for frame in frames
                    if frame.get("name") == "python_worker"
                    and (frame.get("source") or {}).get("path")
                    == str(MULTIPROCESS_WORKER)
                ),
                None,
            )
            if not isinstance(snapshot, dict):
                raise DapError(f"Rust stop had no outer Python frame: {frames[:12]}")
            client.response(
                client.send("scopes", {"frameId": snapshot["id"]}),
                timeout=15,
            )

            deadline = __import__("time").monotonic() + 40
            while True:
                remaining = deadline - __import__("time").monotonic()
                if remaining <= 0:
                    raise DapError(
                        "neighboring user breakpoint removed the debugpy handoff"
                    )
                resumed = client.event("stopped", timeout=remaining)
                body = resumed.get("body", {})
                if (
                    body.get("systemProcessId") == process_id
                    and isinstance(body.get("threadId"), int)
                    and body["threadId"] >= 1_600_000_000
                ):
                    break

            live = _stack(client, resumed["body"]["threadId"])[0]
            live_id = live.get("id")
            if (
                live.get("name") != "python_worker"
                or live.get("line") != 52
                or not isinstance(live_id, int)
            ):
                raise DapError(
                    f"debugpy did not reacquire the selected Python frame: {live}"
                )
            client.response(client.send("scopes", {"frameId": live_id}), timeout=10)

            import_request = client.send(
                "evaluate",
                {"expression": "import sys", "context": "repl"},
            )
            if (
                client.response(import_request, timeout=10)
                .get("body", {})
                .get("result")
                != ""
            ):
                raise DapError("frameless Debug Console import did not use debugpy")
            if _evaluate(
                client,
                live_id,
                "sys.version_info[:2]",
                context="repl",
            ) != "(3, 14)":
                raise DapError("debugpy did not preserve the imported module")
            release = client.response(
                client.send(
                    "evaluate",
                    {
                        "expression": "release",
                        "frameId": live_id,
                        "context": "watch",
                    },
                ),
                timeout=10,
            ).get("body", {})
            if (
                "PosixPath" not in str(release.get("result"))
                or not isinstance(release.get("variablesReference"), int)
            ):
                raise DapError(f"debugpy did not render the Path object: {release}")
        finally:
            client.close()


def dynamic_native_call_hands_off_without_name_discovery() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=PYTHON,
            args=[str(DYNAMIC_CALL_DRIVER)],
        )
        client.event("initialized", timeout=10)
        _set_breakpoint(client, RUST_SOURCE, 6)
        client.response(client.send("configurationDone"), timeout=10)
        client.response(launch, timeout=20)

        rust_stop = client.event("stopped", timeout=30)
        rust_thread = rust_stop.get("body", {}).get("threadId")
        if not isinstance(rust_thread, int):
            raise DapError(f"dynamic-call Rust stop had no thread: {rust_stop}")
        frames = _stack(client, rust_thread)
        routing_frame = next(
            (
                frame
                for frame in frames
                if frame.get("name") == "python_dynamic"
                and (frame.get("source") or {}).get("path")
                == str(DYNAMIC_CALL_DRIVER)
            ),
            None,
        )
        if not isinstance(routing_frame, dict):
            raise DapError(
                f"dynamic-call Rust stop had no Python routing frame: {frames[:12]}"
            )
        client.response(
            client.send("scopes", {"frameId": routing_frame["id"]}),
            timeout=10,
        )

        python_stop = client.event("stopped", timeout=30)
        python_thread = python_stop.get("body", {}).get("threadId")
        if not isinstance(python_thread, int) or python_thread < 1_600_000_000:
            raise DapError(
                f"dynamic-call handoff was not owned by debugpy: {python_stop}"
            )
        live = _stack(client, python_thread)[0]
        live_id = live.get("id")
        if (
            live.get("name") != "python_dynamic"
            or (live.get("source") or {}).get("path") != str(DYNAMIC_CALL_DRIVER)
            or not isinstance(live_id, int)
        ):
            raise DapError(f"dynamic-call debugpy frame was malformed: {live}")
        if (
            _evaluate(client, live_id, "label") != "'dynamic-python-to-rust'"
            or _evaluate(client, live_id, "type(candidates).__name__") != "'dict'"
            or _evaluate(client, live_id, "__import__('sys').version_info[:2]")
            != "(3, 14)"
        ):
            raise DapError("dynamic-call Python frame was not live debugpy")
    finally:
        client.close()


def python_worker_rust_stop_returns_to_same_debugpy_thread() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=PYTHON,
            args=[str(THREADED_DRIVER)],
        )
        client.event("initialized", timeout=10)
        _set_breakpoint(client, RUST_SOURCE, 6)
        client.response(client.send("configurationDone"), timeout=10)
        client.response(launch, timeout=20)

        rust_stop = client.event("stopped", timeout=30)
        rust_thread = rust_stop.get("body", {}).get("threadId")
        if not isinstance(rust_thread, int):
            raise DapError(f"Python-worker Rust stop had no thread: {rust_stop}")
        frames = _stack(client, rust_thread)
        routing_frame = next(
            (frame for frame in frames if frame.get("name") == "python_worker"),
            None,
        )
        if not isinstance(routing_frame, dict):
            raise DapError(f"Python worker frame was missing: {frames[:12]}")
        client.response(
            client.send("scopes", {"frameId": routing_frame["id"]}),
            timeout=10,
        )

        python_stop = client.event("stopped", timeout=30)
        python_thread = python_stop.get("body", {}).get("threadId")
        if not isinstance(python_thread, int) or python_thread < 1_600_000_000:
            raise DapError(
                f"Python worker handoff was not owned by debugpy: {python_stop}"
            )
        live = _stack(client, python_thread)[0]
        live_id = live.get("id")
        if (
            live.get("name") != "python_worker"
            or (live.get("source") or {}).get("path") != str(THREADED_DRIVER)
            or not isinstance(live_id, int)
        ):
            raise DapError(f"Python worker debugpy frame was malformed: {live}")
        label = _evaluate(client, live_id, "worker_label")
        value = _evaluate(client, live_id, "worker_value")
        if (label, value) not in {
            ("'worker-A'", "20"),
            ("'worker-B'", "40"),
        }:
            raise DapError(
                f"Python worker debugpy locals crossed threads: {label}, {value}"
            )
    finally:
        client.close()


def rust_worker_python_frame_uses_same_debugpy_thread() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=RUST_THREADED_BINARY,
            args=[],
            env={"LD_LIBRARY_PATH": _python_libdir()},
        )
        client.event("initialized", timeout=10)
        _set_breakpoint(client, RUST_THREADED_SOURCE, 13)
        client.response(client.send("configurationDone"), timeout=10)
        client.response(launch, timeout=20)

        rust_stop = client.event("stopped", timeout=45)
        rust_thread = rust_stop.get("body", {}).get("threadId")
        if not isinstance(rust_thread, int):
            raise DapError(f"Rust-worker callback stop had no thread: {rust_stop}")
        frames = _stack(client, rust_thread)
        routing_frame = next(
            (
                frame
                for frame in frames
                if frame.get("name") == "python_inner"
                and (frame.get("source") or {}).get("path")
                == str(RUST_THREADED_PYTHON_SOURCE)
            ),
            None,
        )
        if not isinstance(routing_frame, dict):
            raise DapError(
                f"Rust worker had no embedded Python frame: {frames[:16]}"
            )
        client.response(
            client.send("scopes", {"frameId": routing_frame["id"]}),
            timeout=10,
        )

        python_stop = client.event("stopped", timeout=45)
        python_thread = python_stop.get("body", {}).get("threadId")
        if not isinstance(python_thread, int) or python_thread < 1_600_000_000:
            raise DapError(
                f"Rust-worker Python handoff was not debugpy-owned: {python_stop}"
            )
        live = _stack(client, python_thread)[0]
        live_id = live.get("id")
        if (
            live.get("name") != "python_inner"
            or (live.get("source") or {}).get("path")
            != str(RUST_THREADED_PYTHON_SOURCE)
            or not isinstance(live_id, int)
        ):
            raise DapError(f"Rust worker live Python frame was malformed: {live}")
        label = _evaluate(client, live_id, "worker_label")
        value = _evaluate(client, live_id, "worker_value")
        if (label, value) not in {
            ("'rust-worker-A'", "20"),
            ("'rust-worker-B'", "40"),
        }:
            raise DapError(
                f"Rust worker live Python locals crossed threads: {label}, {value}"
            )
    finally:
        client.close()


def rust_worker_selected_rust_frame_steps_on_same_native_thread() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=RUST_THREADED_BINARY,
            args=[],
            env={"LD_LIBRARY_PATH": _python_libdir()},
        )
        client.event("initialized", timeout=10)
        _set_breakpoint(client, RUST_THREADED_SOURCE, 13)
        client.response(client.send("configurationDone"), timeout=10)
        client.response(launch, timeout=20)

        rust_stop = client.event("stopped", timeout=45)
        native_thread = rust_stop.get("body", {}).get("threadId")
        if not isinstance(native_thread, int):
            raise DapError(f"Rust worker stop had no native identity: {rust_stop}")
        routing = next(
            (
                frame
                for frame in _stack(client, native_thread)
                if frame.get("name") == "python_inner"
                and (frame.get("source") or {}).get("path")
                == str(RUST_THREADED_PYTHON_SOURCE)
            ),
            None,
        )
        if not isinstance(routing, dict):
            raise DapError("Rust worker stop had no selectable Python frame")
        client.response(client.send("scopes", {"frameId": routing["id"]}), timeout=10)

        python_stop = client.event("stopped", timeout=45)
        python_thread = python_stop.get("body", {}).get("threadId")
        if not isinstance(python_thread, int) or python_thread < 1_600_000_000:
            raise DapError(f"Rust worker did not transfer to debugpy: {python_stop}")
        live = _stack(client, python_thread)
        rust_frame = next(
            (
                frame
                for frame in live
                if (frame.get("source") or {}).get("path")
                == str(RUST_THREADED_SOURCE)
                and "rust_outer::{closure" in str(frame.get("name", ""))
            ),
            None,
        )
        if not isinstance(rust_frame, dict) or rust_frame.get("line") != 32:
            raise DapError(f"debugpy worker used the wrong native TID: {live[:8]}")
        client.response(
            client.send("scopes", {"frameId": rust_frame["id"]}),
            timeout=10,
        )
        client.response(
            client.send(
                "next",
                {"threadId": python_thread, "granularity": "line"},
            ),
            timeout=10,
        )

        stepped = client.event("stopped", timeout=45)
        stepped_thread = stepped.get("body", {}).get("threadId")
        if (
            stepped.get("body", {}).get("reason") != "step"
            or stepped_thread != native_thread
        ):
            raise DapError(f"Rust worker step changed native identity: {stepped}")
        top = _stack(client, stepped_thread)[0]
        if (
            top.get("line") != 33
            or (top.get("source") or {}).get("path")
            != str(RUST_THREADED_SOURCE)
            or _evaluate(client, top["id"], "1 + 1") != "2"
        ):
            raise DapError(f"Rust worker did not perform a live native step: {top}")
    finally:
        client.close()


def asyncio_task_live_debugpy_returns_to_same_rust_thread() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(client, program=PYTHON, args=[str(ASYNC_DRIVER)])
        client.event("initialized", timeout=10)
        _set_breakpoint(client, RUST_SOURCE, 6)
        client.response(client.send("configurationDone"), timeout=10)
        client.response(launch, timeout=20)

        observed: set[tuple[str, str, str]] = set()
        native_thread: int | None = None
        rust_stop = client.event("stopped", timeout=30)
        for index in range(2):
            current_native = rust_stop.get("body", {}).get("threadId")
            if not isinstance(current_native, int):
                raise DapError(f"async Rust stop had no native thread: {rust_stop}")
            if native_thread is None:
                native_thread = current_native
            elif current_native != native_thread:
                raise DapError("asyncio tasks changed event-loop OS thread")
            routing = next(
                (
                    frame
                    for frame in _stack(client, current_native)
                    if frame.get("name") == "async_worker"
                    and (frame.get("source") or {}).get("path") == str(ASYNC_DRIVER)
                ),
                None,
            )
            if not isinstance(routing, dict):
                raise DapError("async Rust stop had no selectable coroutine frame")
            client.response(
                client.send("scopes", {"frameId": routing["id"]}),
                timeout=10,
            )

            python_stop = client.event("stopped", timeout=30)
            python_thread = python_stop.get("body", {}).get("threadId")
            if not isinstance(python_thread, int) or python_thread < 1_600_000_000:
                raise DapError(f"async coroutine was not debugpy-owned: {python_stop}")
            live = _stack(client, python_thread)[0]
            frame_id = live.get("id")
            if (
                live.get("name") != "async_worker"
                or (live.get("source") or {}).get("path") != str(ASYNC_DRIVER)
                or not isinstance(frame_id, int)
            ):
                raise DapError(f"live async coroutine frame was malformed: {live}")
            observed.add(
                (
                    _evaluate(client, frame_id, "label"),
                    _evaluate(client, frame_id, "value"),
                    _evaluate(client, frame_id, "task_name"),
                )
            )
            if index == 0:
                client.response(
                    client.send("stepIn", {"threadId": python_thread}),
                    timeout=10,
                )
                returned = client.event("stopped", timeout=30)
                returned_thread = returned.get("body", {}).get("threadId")
                top = (
                    _stack(client, returned_thread)[0]
                    if isinstance(returned_thread, int)
                    else {}
                )
                if (
                    returned.get("body", {}).get("reason") != "step"
                    or returned_thread != native_thread
                    or (top.get("source") or {}).get("path") != str(RUST_SOURCE)
                    or "rust_inner" not in str(top.get("name", ""))
                ):
                    raise DapError(
                        f"async Python-to-Rust step lost ownership: {returned}"
                    )
                rust_stop = returned
            else:
                client.response(
                    client.send(
                        "continue",
                        {"threadId": python_thread, "singleThread": True},
                    ),
                    timeout=10,
                )
        if observed != {
            ("'async-A'", "20", "'async-A'"),
            ("'async-B'", "40", "'async-B'"),
        }:
            raise DapError(f"live debugpy async task state crossed tasks: {observed}")
    finally:
        client.close()


def rust_futures_use_live_debugpy_python_stepping() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=RUST_ASYNC_BINARY,
            args=[],
            env={"LD_LIBRARY_PATH": _python_libdir()},
        )
        client.event("initialized", timeout=10)
        _set_breakpoint(client, RUST_ASYNC_SOURCE, 14)
        client.response(client.send("configurationDone"), timeout=10)
        client.response(launch, timeout=20)

        observed: set[tuple[str, str]] = set()
        for index, command in enumerate(("next", "stepOut")):
            rust_stop = client.event("stopped", timeout=30)
            native_thread = rust_stop.get("body", {}).get("threadId")
            if not isinstance(native_thread, int):
                raise DapError(f"Rust future stop had no native thread: {rust_stop}")
            routing = next(
                (
                    frame
                    for frame in _stack(client, native_thread)
                    if frame.get("name") == "python_inner"
                    and (frame.get("source") or {}).get("path")
                    == str(RUST_ASYNC_PYTHON_SOURCE)
                ),
                None,
            )
            if not isinstance(routing, dict):
                raise DapError("Rust future had no selectable Python coroutine")
            client.response(
                client.send("scopes", {"frameId": routing["id"]}),
                timeout=10,
            )

            python_stop = client.event("stopped", timeout=30)
            python_thread = python_stop.get("body", {}).get("threadId")
            if not isinstance(python_thread, int) or python_thread < 1_600_000_000:
                raise DapError(f"Rust future was not debugpy-owned: {python_stop}")
            live = _stack(client, python_thread)[0]
            frame_id = live.get("id")
            if (
                live.get("name") != "python_inner"
                or (live.get("source") or {}).get("path")
                != str(RUST_ASYNC_PYTHON_SOURCE)
                or not isinstance(frame_id, int)
            ):
                raise DapError(f"Rust future live Python frame was malformed: {live}")
            observed.add(
                (
                    _evaluate(client, frame_id, "worker_label"),
                    _evaluate(client, frame_id, "worker_value"),
                )
            )
            client.response(client.send(command, {"threadId": python_thread}), timeout=10)
            stepped = client.event("stopped", timeout=30)
            stepped_thread = stepped.get("body", {}).get("threadId")
            if not isinstance(stepped_thread, int) or stepped_thread < 1_600_000_000:
                raise DapError(f"Rust future {command} left debugpy: {stepped}")
            top = _stack(client, stepped_thread)[0]
            expected = ("python_inner", 10) if command == "next" else ("python_outer", 16)
            if (
                stepped.get("body", {}).get("reason") != "step"
                or (top.get("name"), top.get("line")) != expected
                or (top.get("source") or {}).get("path")
                != str(RUST_ASYNC_PYTHON_SOURCE)
            ):
                raise DapError(f"Rust future {command} reached the wrong frame: {top}")
            if index == 0:
                client.response(
                    client.send(
                        "continue",
                        {"threadId": stepped_thread, "singleThread": True},
                    ),
                    timeout=10,
                )
        if observed != {
            ("'rust-async-A'", "20"),
            ("'rust-async-B'", "40"),
        }:
            raise DapError(f"live Rust-future Python state crossed futures: {observed}")
    finally:
        client.close()


def rust_future_selected_rust_poll_frame_steps_in_codelldb() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=RUST_ASYNC_BINARY,
            args=[],
            env={"LD_LIBRARY_PATH": _python_libdir()},
        )
        client.event("initialized", timeout=10)
        _set_breakpoint(client, RUST_ASYNC_SOURCE, 14)
        client.response(client.send("configurationDone"), timeout=10)
        client.response(launch, timeout=20)

        rust_stop = client.event("stopped", timeout=30)
        native_thread = rust_stop.get("body", {}).get("threadId")
        if not isinstance(native_thread, int):
            raise DapError(f"Rust async stop had no native thread: {rust_stop}")
        routing = next(
            (frame for frame in _stack(client, native_thread) if frame.get("name") == "python_inner"),
            None,
        )
        if not isinstance(routing, dict):
            raise DapError("Rust async stop had no Python routing frame")
        client.response(client.send("scopes", {"frameId": routing["id"]}), timeout=10)

        python_stop = client.event("stopped", timeout=30)
        python_thread = python_stop.get("body", {}).get("threadId")
        if not isinstance(python_thread, int):
            raise DapError(f"Rust async handoff had no Python thread: {python_stop}")
        live = _stack(client, python_thread)
        rust_frame = next(
            (
                frame
                for frame in live
                if (frame.get("source") or {}).get("path") == str(RUST_ASYNC_SOURCE)
                and frame.get("line") == 56
                and "rust_outer" in str(frame.get("name", ""))
            ),
            None,
        )
        if not isinstance(rust_frame, dict):
            raise DapError(f"live Rust async poll frame was missing: {live[:16]}")
        client.response(
            client.send("scopes", {"frameId": rust_frame["id"]}),
            timeout=10,
        )
        client.response(
            client.send(
                "next",
                {"threadId": python_thread, "granularity": "line"},
            ),
            timeout=10,
        )
        stepped = client.event("stopped", timeout=30)
        stepped_thread = stepped.get("body", {}).get("threadId")
        top = _stack(client, stepped_thread)[0] if isinstance(stepped_thread, int) else {}
        if (
            stepped.get("body", {}).get("reason") != "step"
            or stepped_thread != native_thread
            or top.get("line") != 57
            or (top.get("source") or {}).get("path") != str(RUST_ASYNC_SOURCE)
            or _evaluate(client, top["id"], "1 + 1") != "2"
        ):
            raise DapError(f"Rust async poll frame did not step in CodeLLDB: {top}")
    finally:
        client.close()


def arbitrary_pyo3_names_merge_and_handoff_to_debugpy() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=PYTHON,
            args=[str(ARBITRARY_BOUNDARY_DRIVER)],
        )
        client.event("initialized", timeout=10)
        _set_breakpoint(client, RUST_SOURCE, 39)
        client.response(client.send("configurationDone"), timeout=10)
        client.response(launch, timeout=20)

        rust_stop = client.event("stopped", timeout=30)
        native_thread = rust_stop.get("body", {}).get("threadId")
        if not isinstance(native_thread, int):
            raise DapError(f"arbitrary-name Rust stop had no thread: {rust_stop}")
        frames = _stack(client, native_thread)
        names = [str(frame.get("name", "")) for frame in frames]
        if not (
            any("calculate_leaf" in name for name in names)
            and any("dispatch_payload" in name for name in names)
        ):
            raise DapError(f"arbitrary Rust callees were missing: {names[:12]}")
        routing = next(
            (
                frame
                for frame in frames
                if frame.get("name") == "handle_event"
                and (frame.get("source") or {}).get("path")
                == str(ARBITRARY_BOUNDARY_DRIVER)
            ),
            None,
        )
        if not isinstance(routing, dict):
            raise DapError(f"arbitrary Python caller was not merged: {names[:12]}")
        client.response(client.send("scopes", {"frameId": routing["id"]}), timeout=10)

        python_stop = client.event("stopped", timeout=45)
        python_thread = python_stop.get("body", {}).get("threadId")
        if not isinstance(python_thread, int) or python_thread < 1_600_000_000:
            raise DapError(f"arbitrary Python caller was not debugpy-owned: {python_stop}")
        live = _stack(client, python_thread)[0]
        frame_id = live.get("id")
        if (
            live.get("name") != "handle_event"
            or not isinstance(frame_id, int)
            or _evaluate(client, frame_id, "request_label") != "'arbitrary-boundary'"
            or _evaluate(client, frame_id, "request_value") != "35"
        ):
            raise DapError(f"arbitrary Python caller was not live: {live}")
    finally:
        client.close()


def _selected_rust_frame_step(command: str) -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=RUST_OUTER_BINARY,
            args=[],
            env={"LD_LIBRARY_PATH": _python_libdir()},
        )
        client.event("initialized", timeout=10)
        _set_breakpoint(client, EMBEDDED_PYTHON_SOURCE, 4)
        client.response(client.send("configurationDone"), timeout=10)
        client.response(launch, timeout=20)

        python_stop = client.event("stopped", timeout=30)
        python_thread = python_stop.get("body", {}).get("threadId")
        if not isinstance(python_thread, int):
            raise DapError(f"Rust-step Python stop had no thread: {python_stop}")
        frames = _stack(client, python_thread)
        rust_frame = next(
            (
                frame
                for frame in frames
                if (frame.get("source") or {}).get("path")
                == str(RUST_OUTER_SOURCE)
                and "closure" in str(frame.get("name", ""))
            ),
            None,
        )
        if not isinstance(rust_frame, dict):
            raise DapError(f"Python stop had no active Rust call frame: {frames}")
        rust_id = rust_frame.get("id")
        if not isinstance(rust_id, int):
            raise DapError(f"Rust lease frame had no ID: {rust_frame}")
        original_line = rust_frame.get("line")
        original_name = rust_frame.get("name")
        if command == "stepOut":
            cleared = client.send(
                "setBreakpoints",
                {
                    "source": {"path": str(EMBEDDED_PYTHON_SOURCE)},
                    "breakpoints": [],
                    "sourceModified": False,
                },
            )
            client.response(cleared, timeout=10)
        client.response(client.send("scopes", {"frameId": rust_id}), timeout=10)

        step = client.send(
            command,
            {"threadId": python_thread, "granularity": "line"},
        )
        client.response(step, timeout=10)
        rust_stop = client.event("stopped", timeout=30)
        rust_thread = rust_stop.get("body", {}).get("threadId")
        if (
            rust_stop.get("body", {}).get("reason") != "step"
            or not isinstance(rust_thread, int)
            or rust_thread >= 1_600_000_000
        ):
            raise DapError(
                f"selected Rust step did not return to CodeLLDB: {rust_stop}"
            )
        live = _stack(client, rust_thread)
        top = live[0] if live else {}
        live_id = top.get("id") if isinstance(top, dict) else None
        if not isinstance(live_id, int) or _evaluate(client, live_id, "1 + 1") != "2":
            raise DapError(
                f"CodeLLDB did not own the returned Rust frame: {top}"
            )
        if command in {"next", "stepIn"}:
            if (
                top.get("name") != original_name
                or top.get("line") != 30
                or top.get("line") == original_line
            ):
                raise DapError(f"selected Rust {command} did not advance: {top}")
        elif any(frame.get("name") == original_name for frame in live):
            raise DapError(f"selected Rust stepOut retained its frame: {live[:6]}")
    finally:
        client.close()


def selected_rust_frame_step_returns_to_codelldb() -> None:
    _selected_rust_frame_step("next")


def selected_rust_frame_step_in_returns_to_codelldb() -> None:
    _selected_rust_frame_step("stepIn")


def selected_rust_frame_step_out_returns_to_codelldb() -> None:
    _selected_rust_frame_step("stepOut")


def selected_python_frame_step_in_returns_to_codelldb() -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=RUST_OUTER_BINARY,
            args=[],
            env={"LD_LIBRARY_PATH": _python_libdir()},
        )
        client.event("initialized", timeout=10)
        _set_breakpoint(client, RUST_OUTER_SOURCE, 8)
        client.response(client.send("configurationDone"), timeout=10)
        client.response(launch, timeout=20)

        rust_stop = client.event("stopped", timeout=30)
        rust_thread = rust_stop.get("body", {}).get("threadId")
        if not isinstance(rust_thread, int):
            raise DapError(f"Python-step Rust stop had no thread: {rust_stop}")
        python_frame = next(
            (
                frame
                for frame in _stack(client, rust_thread)
                if frame.get("name") == "python_inner"
                and (frame.get("source") or {}).get("path")
                == str(EMBEDDED_PYTHON_SOURCE)
            ),
            None,
        )
        if not isinstance(python_frame, dict):
            raise DapError("Rust stop had no selectable Python frame")
        client.response(
            client.send("scopes", {"frameId": python_frame["id"]}),
            timeout=10,
        )

        python_stop = client.event("stopped", timeout=30)
        python_thread = python_stop.get("body", {}).get("threadId")
        if not isinstance(python_thread, int) or python_thread < 1_600_000_000:
            raise DapError(
                f"selected Python frame was not owned by debugpy: {python_stop}"
            )
        live_python = _stack(client, python_thread)[0]
        if live_python.get("name") != "python_inner":
            raise DapError(f"debugpy selected the wrong Python frame: {live_python}")

        client.response(
            client.send("stepIn", {"threadId": python_thread}),
            timeout=10,
        )
        stepped = client.event("stopped", timeout=30)
        stepped_thread = stepped.get("body", {}).get("threadId")
        if (
            stepped.get("body", {}).get("reason") != "step"
            or not isinstance(stepped_thread, int)
            or stepped_thread >= 1_600_000_000
        ):
            raise DapError(
                f"Python-to-Rust step did not return to CodeLLDB: {stepped}"
            )
        live_rust = _stack(client, stepped_thread)
        top = live_rust[0] if live_rust else {}
        if (
            top.get("name") != "rust_outer_python_inner::rust_callback"
            or (top.get("source") or {}).get("path") != str(RUST_OUTER_SOURCE)
        ):
            raise DapError(f"Python-to-Rust step stopped in the wrong frame: {top}")
        if not any(frame.get("name") == "python_inner" for frame in live_rust):
            raise DapError("Python-to-Rust step lost the Python caller")
    finally:
        client.close()


def _selected_python_handoff_step(command: str, line: int, name: str) -> None:
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=RUST_OUTER_BINARY,
            args=[],
            env={"LD_LIBRARY_PATH": _python_libdir()},
        )
        client.event("initialized", timeout=10)
        _set_breakpoint(client, RUST_OUTER_SOURCE, 8)
        client.response(client.send("configurationDone"), timeout=10)
        client.response(launch, timeout=20)

        rust_stop = client.event("stopped", timeout=30)
        rust_thread = rust_stop.get("body", {}).get("threadId")
        if not isinstance(rust_thread, int):
            raise DapError(f"Python {command} Rust stop had no thread: {rust_stop}")
        routing = next(
            (frame for frame in _stack(client, rust_thread) if frame.get("name") == "python_inner"),
            None,
        )
        if not isinstance(routing, dict):
            raise DapError("Rust stop had no selectable Python frame")
        client.response(client.send("scopes", {"frameId": routing["id"]}), timeout=10)

        python_stop = client.event("stopped", timeout=30)
        python_thread = python_stop.get("body", {}).get("threadId")
        if not isinstance(python_thread, int) or python_thread < 1_600_000_000:
            raise DapError(f"Python {command} handoff was not debugpy-owned")
        client.response(client.send(command, {"threadId": python_thread}), timeout=10)

        stepped = client.event("stopped", timeout=30)
        stepped_thread = stepped.get("body", {}).get("threadId")
        if not isinstance(stepped_thread, int) or stepped_thread < 1_600_000_000:
            raise DapError(f"Python {command} left debugpy ownership: {stepped}")
        top = _stack(client, stepped_thread)[0]
        if (
            stepped.get("body", {}).get("reason") != "step"
            or top.get("name") != name
            or top.get("line") != line
            or (top.get("source") or {}).get("path") != str(EMBEDDED_PYTHON_SOURCE)
        ):
            raise DapError(f"Python {command} reached the wrong frame: {top}")
        if _evaluate(client, top["id"], "value") not in {"20", "21"}:
            raise DapError(f"Python {command} did not retain live debugpy locals")
    finally:
        client.close()


def selected_python_frame_next_stays_in_debugpy() -> None:
    _selected_python_handoff_step("next", 5, "python_inner")


def selected_python_frame_step_out_stays_in_debugpy() -> None:
    _selected_python_handoff_step("stepOut", 12, "python_outer")


def main() -> int:
    cases = (
        ("AC-DP-01", lambda: full_python_evaluation()[0].close()),
        ("AC-DP-02", python_stop_and_native_handoff),
        ("AC-DP-03", python_threads),
        ("AC-DP-04", python_processes),
        ("AC-DP-05", rust_outer_python_stop),
        ("AC-DP-06", python_step_in),
        ("AC-DP-07", rust_outer_restart),
        ("AC-DP-08", rust_outer_cross_language_step_in),
        ("AC-DP-09", live_python_assignment),
        ("AC-DP-10", live_rust_assignment),
        ("AC-DP-11", rust_stop_to_live_debugpy_frame),
        ("AC-DP-12", python_stop_to_live_codelldb_frame),
        ("AC-DP-13", child_rust_stop_to_live_debugpy_frame),
        ("AC-DP-14", child_user_breakpoint_preserves_debugpy_handoff),
        ("AC-DP-15", dynamic_native_call_hands_off_without_name_discovery),
        ("AC-DP-16", python_worker_rust_stop_returns_to_same_debugpy_thread),
        ("AC-DP-17", rust_worker_python_frame_uses_same_debugpy_thread),
        ("AC-DP-18", selected_rust_frame_step_returns_to_codelldb),
        ("AC-DP-19", selected_python_frame_step_in_returns_to_codelldb),
        ("AC-DP-20", selected_python_frame_next_stays_in_debugpy),
        ("AC-DP-21", selected_python_frame_step_out_stays_in_debugpy),
        ("AC-DP-22", selected_rust_frame_step_in_returns_to_codelldb),
        ("AC-DP-23", selected_rust_frame_step_out_returns_to_codelldb),
        ("AC-DP-24", rust_worker_selected_rust_frame_steps_on_same_native_thread),
        ("AC-DP-25", asyncio_task_live_debugpy_returns_to_same_rust_thread),
        ("AC-DP-26", rust_futures_use_live_debugpy_python_stepping),
        ("AC-DP-27", rust_future_selected_rust_poll_frame_steps_in_codelldb),
        ("AC-DP-28", arbitrary_pyo3_names_merge_and_handoff_to_debugpy),
    )
    results: dict[str, bool] = {}
    for criterion, case in cases:
        try:
            case()
            results[criterion] = True
        except (DapError, OSError, TimeoutError, ValueError) as error:
            results[criterion] = False
            print(f"{criterion} failure: {error}")
    for criterion, passed in results.items():
        print(f"{criterion} {'PASS' if passed else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
