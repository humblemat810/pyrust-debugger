"""Small DAP transport and contract helpers used by acceptance tests.

The real acceptance path talks only to the proxy's stdio DAP boundary. These
helpers deliberately know nothing about the proxy implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import select
import shlex
import signal
import subprocess
import time
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "bin" / "python"
FIXTURE = ROOT / "research" / "fixtures" / "python_outer"
RUST_SOURCE = FIXTURE / "src" / "lib.rs"
PYTHON_SOURCE = FIXTURE / "app.py"
DRIVER = Path(__file__).resolve().with_name("fixture_driver.py")


class DapError(RuntimeError):
    """A bounded, layer-specific DAP failure."""


def encode_message(message: dict[str, Any]) -> bytes:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n" + payload


class DapStreamParser:
    """Incremental parser for fragmented and coalesced DAP messages."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self.buffer.extend(data)
        messages: list[dict[str, Any]] = []
        while True:
            separator = self.buffer.find(b"\r\n\r\n")
            if separator < 0:
                return messages
            header = bytes(self.buffer[:separator]).split(b"\r\n")
            length: int | None = None
            for line in header:
                name, _, value = line.partition(b":")
                if name.lower() == b"content-length":
                    try:
                        length = int(value.strip())
                    except ValueError as error:
                        raise DapError("DAP framing: invalid Content-Length") from error
            if length is None or length < 0:
                raise DapError("DAP framing: missing or negative Content-Length")
            body_start = separator + 4
            body_end = body_start + length
            if len(self.buffer) < body_end:
                return messages
            body = bytes(self.buffer[body_start:body_end])
            del self.buffer[:body_end]
            try:
                message = json.loads(body)
            except json.JSONDecodeError as error:
                raise DapError("DAP framing: invalid JSON payload") from error
            if not isinstance(message, dict):
                raise DapError("DAP framing: message is not an object")
            messages.append(message)


class DapClient:
    """A bounded stdio DAP client suitable for the proxy black box."""

    def __init__(self, command: list[str], env: dict[str, str] | None = None) -> None:
        self.process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env or os.environ.copy(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert self.process.stdin and self.process.stdout and self.process.stderr
        self.stdin = self.process.stdin
        self.stdout = self.process.stdout
        self.stderr = self.process.stderr
        self.parser = DapStreamParser()
        self.pending: list[dict[str, Any]] = []
        self.deferred: list[dict[str, Any]] = []
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
        try:
            self.stdin.write(encode_message(message))
            self.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise DapError(f"proxy transport write failed: {error}") from error
        return sequence

    def read(self, timeout: float = 10.0) -> dict[str, Any]:
        if self.pending:
            return self.pending.pop(0)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DapError("proxy protocol timeout")
            ready, _, _ = select.select([self.stdout], [], [], remaining)
            if not ready:
                raise DapError("proxy protocol timeout")
            chunk = os.read(self.stdout.fileno(), 65536)
            if not chunk:
                self.process.terminate()
                try:
                    _, diagnostic_bytes = self.process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    _, diagnostic_bytes = self.process.communicate(timeout=1)
                diagnostic = diagnostic_bytes.decode("utf-8", "replace")
                raise DapError(
                    f"proxy closed its DAP stream: {diagnostic[-1000:] or 'no diagnostic'}"
                )
            messages = self.parser.feed(chunk)
            if messages:
                self.messages.extend(messages)
                self.pending.extend(messages[1:])
                return messages[0]

    def wait_for(
        self,
        predicate: Any,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            for index, message in enumerate(self.deferred):
                if predicate(message):
                    return self.deferred.pop(index)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DapError("proxy protocol timeout while waiting for message")
            message = self.read(remaining)
            if predicate(message):
                return message
            self.deferred.append(message)

    def response(self, request_sequence: int, timeout: float = 10.0) -> dict[str, Any]:
        message = self.wait_for(
            lambda item: item.get("type") == "response"
            and item.get("request_seq") == request_sequence,
            timeout=timeout,
        )
        if not message.get("success", False):
            raise DapError(f"DAP request failed: {message}")
        return message

    def event(self, name: str, timeout: float = 10.0) -> dict[str, Any]:
        return self.wait_for(
            lambda item: item.get("type") == "event" and item.get("event") == name,
            timeout=timeout,
        )

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                request = self.send("disconnect", {"terminateDebuggee": True})
                self.response(request, timeout=3)
            except (DapError, BrokenPipeError, OSError):
                self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self._terminate_process_group()
        for stream in (self.stdin, self.stdout, self.stderr):
            try:
                stream.close()
            except OSError:
                pass

    def _terminate_process_group(self) -> None:
        """Reap debugger descendants before the next black-box session."""

        if not hasattr(os, "killpg"):
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            try:
                os.killpg(self.process.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.01)
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def proxy_command() -> list[str]:
    configured = os.environ.get("PYRUST_DAP_PROXY")
    if configured:
        return shlex.split(configured)
    candidates = (
        ROOT / "prototype" / "adapter" / "__main__.py",
        ROOT / "prototype" / "adapter" / "main.py",
        ROOT / "prototype" / "adapter" / "pyrust_adapter.py",
    )
    for candidate in candidates:
        if candidate.is_file():
            return [str(PYTHON), str(candidate)]
    raise DapError(
        "proxy command is not configured; set PYRUST_DAP_PROXY to the stdio "
        "DAP proxy command"
    )


def launch_arguments(
    *,
    helper_command: str | None = None,
    helper_timeout_ms: int | None = None,
    program: Path | None = None,
    args: list[str] | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "program": str(program or PYTHON),
        "args": args if args is not None else [str(DRIVER)],
        "cwd": str(ROOT),
        "terminal": "console",
        "consoleMode": "evaluate",
        "sourceLanguages": ["rust"],
        # Legacy suites explicitly verify the snapshot fallback.
        "pyrustPythonDebug": False,
    }
    if helper_command is not None:
        arguments["pyrustHelperCommand"] = helper_command
    if helper_timeout_ms is not None:
        arguments["pyrustHelperTimeoutMs"] = helper_timeout_ms
    return arguments


def user_frames(stack_response: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        frame
        for frame in stack_response.get("body", {}).get("stackFrames", [])
        if frame.get("name")
        in {"rust_inner", "pyrust_native::rust_inner", "rust_outer", "pyrust_native::rust_outer",
            "python_inner", "python_outer"}
    ]


def names(frames: Iterable[dict[str, Any]]) -> list[str]:
    result = []
    for frame in frames:
        name = frame.get("name", "")
        if "::" in name:
            name = name.rsplit("::", 1)[-1]
        result.append(name)
    return result


@dataclass(frozen=True)
class EpochFrames:
    epoch: int
    frame_ids: frozenset[int]
