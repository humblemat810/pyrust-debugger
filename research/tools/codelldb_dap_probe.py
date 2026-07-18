"""Capture CodeLLDB and CPython stack evidence for the two research fixtures."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
PYTHON_PROTOTYPE = ROOT / "prototype" / "python"
sys.path.insert(0, str(PYTHON_PROTOTYPE))

from pyrust_stack import read_python_stacks  # noqa: E402


class DapClient:
    def __init__(self, command: list[str], env: dict[str, str]) -> None:
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.stdin = self.process.stdin
        self.stdout = self.process.stdout
        self.sequence = 1
        self.messages: list[dict[str, Any]] = []

    def send(self, command: str, arguments: dict[str, Any] | None = None) -> int:
        sequence = self.sequence
        self.sequence += 1
        message: dict[str, Any] = {
            "seq": sequence,
            "type": "request",
            "command": command,
        }
        if arguments is not None:
            message["arguments"] = arguments
        payload = json.dumps(message, separators=(",", ":")).encode()
        self.stdin.write(f"Content-Length: {len(payload)}\r\n\r\n".encode())
        self.stdin.write(payload)
        self.stdin.flush()
        return sequence

    def read(self) -> dict[str, Any]:
        headers: dict[str, str] = {}
        while True:
            line = self.stdout.readline()
            if not line:
                stderr = self.process.stderr.read().decode() if self.process.stderr else ""
                raise RuntimeError(f"CodeLLDB closed its output: {stderr}")
            if line == b"\r\n":
                break
            name, value = line.decode().split(":", 1)
            headers[name.lower()] = value.strip()
        payload = self.stdout.read(int(headers["content-length"]))
        message = json.loads(payload)
        self.messages.append(message)
        return message

    def wait_for_response(self, request_sequence: int) -> dict[str, Any]:
        while True:
            message = self.read()
            if (
                message.get("type") == "response"
                and message.get("request_seq") == request_sequence
            ):
                if not message.get("success"):
                    raise RuntimeError(f"DAP request failed: {message}")
                return message

    def wait_for_event(self, event: str) -> dict[str, Any]:
        while True:
            message = self.read()
            if message.get("type") == "event" and message.get("event") == event:
                return message

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                sequence = self.send("disconnect", {"terminateDebuggee": True})
                self.wait_for_response(sequence)
            except (BrokenPipeError, RuntimeError):
                self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def find_codelldb_extension() -> Path:
    candidates = sorted(
        (Path.home() / ".vscode-server" / "extensions").glob(
            "vadimcn.vscode-lldb-*"
        )
    )
    for candidate in reversed(candidates):
        if (candidate / "adapter" / "codelldb").is_file():
            return candidate
    raise RuntimeError("CodeLLDB platform package is not installed")


def fixture_configuration(case: str) -> tuple[dict[str, Any], str, int]:
    if case == "python-outer":
        source = ROOT / "research" / "fixtures" / "python_outer" / "src" / "lib.rs"
        launch = {
            "program": str(ROOT / ".venv" / "bin" / "python"),
            "args": [str(ROOT / "research" / "fixtures" / "python_outer" / "app.py")],
            "cwd": str(ROOT),
            "terminal": "console",
            "sourceLanguages": ["rust"],
        }
        return launch, str(source), 6

    source = ROOT / "research" / "fixtures" / "rust_outer" / "src" / "main.rs"
    configured_target = os.environ.get("CARGO_TARGET_DIR")
    cargo_target = (
        Path(configured_target)
        if configured_target
        else ROOT / "research" / "fixtures" / "rust_outer" / "target"
    )
    if not cargo_target.is_absolute():
        cargo_target = ROOT / cargo_target
    python_libdir = subprocess.check_output(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            "-c",
            "import sysconfig; print(sysconfig.get_config_var('LIBDIR'))",
        ],
        text=True,
    ).strip()
    launch = {
        "program": str(
            cargo_target / "debug" / "rust-outer-python-inner"
        ),
        "args": [],
        "cwd": str(ROOT),
        "env": {"LD_LIBRARY_PATH": python_libdir},
        "terminal": "console",
        "sourceLanguages": ["rust"],
    }
    return launch, str(source), 9


def run(case: str, mock_frame_provider: bool = False) -> dict[str, Any]:
    extension = find_codelldb_extension()
    command = [
        str(extension / "adapter" / "codelldb"),
        "--liblldb",
        str(extension / "lldb" / "lib" / "liblldb.so"),
    ]
    env = os.environ.copy()
    env["DEBUGINFOD_URLS"] = ""
    client = DapClient(command, env)
    launch, source_path, line = fixture_configuration(case)
    if mock_frame_provider:
        provider_path = ROOT / "research" / "tools" / "mock_scripted_frame_provider.py"
        launch["preRunCommands"] = [
            f"command script import {provider_path}",
            (
                "target frame-provider register "
                "-C mock_scripted_frame_provider.MockPythonFrameProvider "
                f"-k source_path -v {ROOT / 'research' / 'fixtures' / 'python_outer' / 'app.py'}"
            ),
        ]

    try:
        initialize_sequence = client.send(
            "initialize",
            {
                "clientID": "pyrust-research",
                "adapterID": "lldb",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsRunInTerminalRequest": False,
            },
        )
        initialize = client.wait_for_response(initialize_sequence)

        client.send("launch", launch)
        client.wait_for_event("initialized")

        breakpoint_sequence = client.send(
            "setBreakpoints",
            {
                "source": {"path": source_path},
                "breakpoints": [{"line": line}],
                "sourceModified": False,
            },
        )
        breakpoint_response = client.wait_for_response(breakpoint_sequence)

        configuration_sequence = client.send("configurationDone")
        client.wait_for_response(configuration_sequence)
        stopped = client.wait_for_event("stopped")

        process_events = [
            message
            for message in client.messages
            if message.get("type") == "event" and message.get("event") == "process"
        ]
        if process_events and process_events[-1].get("body", {}).get("systemProcessId"):
            pid = process_events[-1]["body"]["systemProcessId"]
            pid_source = "processEvent"
        else:
            status_message_start = len(client.messages)
            status_sequence = client.send(
                "evaluate",
                {"expression": "process status", "context": "repl"},
            )
            status_response = client.wait_for_response(status_sequence)
            status_output = [
                message.get("body", {}).get("output", "")
                for message in client.messages[status_message_start:]
                if message.get("type") == "event" and message.get("event") == "output"
            ]
            status_text = (
                status_response.get("body", {}).get("result", "")
                + "\n"
                + "\n".join(status_output)
            )
            match = re.search(r"Process\s+(\d+)", status_text)
            if not match:
                raise RuntimeError(
                    "Could not discover process ID from CodeLLDB: "
                    f"response={status_response}, output={status_text!r}, "
                    f"messages={client.messages[status_message_start:]}"
                )
            pid = int(match.group(1))
            pid_source = "evaluate: process status"

        threads_sequence = client.send("threads")
        threads_response = client.wait_for_response(threads_sequence)
        stopped_thread_id = stopped["body"]["threadId"]

        stack_sequence = client.send(
            "stackTrace",
            {"threadId": stopped_thread_id, "startFrame": 0, "levels": 40},
        )
        stack_response = client.wait_for_response(stack_sequence)
        python_stacks = [stack.to_dict() for stack in read_python_stacks(pid)]

        return {
            "case": case,
            "mockFrameProvider": mock_frame_provider,
            "adapter": str(command[0]),
            "initializeCapabilities": initialize.get("body", {}),
            "processEvent": process_events[-1] if process_events else None,
            "pid": pid,
            "pidSource": pid_source,
            "stoppedEvent": stopped,
            "breakpointResponse": breakpoint_response,
            "threadsResponse": threads_response,
            "stackTraceResponse": stack_response,
            "pythonStacksWhileStopped": python_stacks,
        }
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=("python-outer", "rust-outer"))
    parser.add_argument("--mock-frame-provider", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.case, args.mock_frame_provider)
    rendered = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
