"""Black-box acceptance for Python-owned debugpy stops in a PyRust session."""

from __future__ import annotations

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
RUST_OUTER_FIXTURE = ROOT / "research" / "fixtures" / "rust_outer"
RUST_OUTER_BINARY = RUST_OUTER_FIXTURE / "target" / "debug" / "rust-outer-python-inner"
RUST_OUTER_SOURCE = RUST_OUTER_FIXTURE / "src" / "main.rs"
EMBEDDED_PYTHON_SOURCE = RUST_OUTER_FIXTURE / "src" / "embedded.py"

CRITERIA = (
    "AC-DP-01",
    "AC-DP-02",
    "AC-DP-03",
    "AC-DP-04",
    "AC-DP-05",
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
    python_libdir = subprocess.check_output(
        [
            str(PYTHON),
            "-c",
            "import sysconfig; print(sysconfig.get_config_var('LIBDIR'))",
        ],
        text=True,
        timeout=5,
    ).strip()
    client = DapClient(proxy_command())
    try:
        initialize(client)
        launch = _launch(
            client,
            program=RUST_OUTER_BINARY,
            args=[],
            env={"LD_LIBRARY_PATH": python_libdir},
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


def main() -> int:
    cases = (
        ("AC-DP-01", lambda: full_python_evaluation()[0].close()),
        ("AC-DP-02", python_stop_and_native_handoff),
        ("AC-DP-03", python_threads),
        ("AC-DP-04", python_processes),
        ("AC-DP-05", rust_outer_python_stop),
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
