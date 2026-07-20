"""Internal CodeLLDB transports for explicitly registered child processes."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
from threading import Event, Lock, Thread
from typing import Any, Callable, Mapping, Sequence

from .framing import DapProtocolError, DapReader, DapWriter


Message = dict[str, Any]
EventHandler = Callable[[Message], None]


class ChildTransportError(RuntimeError):
    """A child CodeLLDB transport could not complete a bounded operation."""


@dataclass
class _PendingRequest:
    command: str
    complete: Event
    response: Message | None = None
    error: BaseException | None = None


class ChildCodelldbTransport:
    """One private DAP connection that attaches CodeLLDB to a child process."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str,
        event_handler: EventHandler,
        timeout: float = 10.0,
    ) -> None:
        self._command = list(command)
        self._cwd = cwd
        self._event_handler = event_handler
        self._timeout = timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._writer: DapWriter | None = None
        self._sequence = 1
        self._sequence_lock = Lock()
        self._pending_lock = Lock()
        self._pending: dict[int, _PendingRequest] = {}
        self._stop = Event()
        self._reader: Thread | None = None
        self._startup_events: Queue[Message] = Queue()

    def start(
        self,
        *,
        process_id: int,
        breakpoints: Sequence[Mapping[str, Any]],
    ) -> None:
        if self._process is not None:
            return
        self._process = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=self._cwd,
            env={**os.environ, "DEBUGINFOD_URLS": ""},
            bufsize=0,
        )
        if self._process.stdin is None or self._process.stdout is None:
            raise ChildTransportError("child CodeLLDB pipes were not created")
        self._writer = DapWriter(self._process.stdin)
        self._reader = Thread(
            target=self._reader_loop,
            name=f"pyrust-child-codelldb-{process_id}",
            daemon=True,
        )
        self._reader.start()

        self.request(
            "initialize",
            {
                "clientID": "pyrust-child-coordinator",
                "adapterID": "lldb",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsRunInTerminalRequest": False,
            },
        )
        # CodeLLDB's attach response is intentionally asynchronous. It becomes
        # complete after configurationDone and the first explicit continue.
        self.send(
            "attach",
            {"pid": process_id, "stopOnEntry": False, "sourceLanguages": ["rust"]},
        )
        # Like launch, CodeLLDB sends initialized only after it has accepted
        # the attach request; waiting before attach deadlocks this transport.
        self._wait_for_event("initialized")
        for breakpoint in breakpoints:
            self.request("setBreakpoints", dict(breakpoint))
        self.request("configurationDone")
        self.request("continue", {"threadId": process_id, "singleThread": False})

    def send(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> int:
        writer = self._writer
        if writer is None or self._stop.is_set():
            raise ChildTransportError("child CodeLLDB transport is unavailable")
        with self._sequence_lock:
            sequence = self._sequence
            self._sequence += 1
        pending = _PendingRequest(command=command, complete=Event())
        with self._pending_lock:
            self._pending[sequence] = pending
        request: Message = {"seq": sequence, "type": "request", "command": command}
        if arguments is not None:
            request["arguments"] = dict(arguments)
        try:
            writer.write_message(request)
        except OSError as error:
            with self._pending_lock:
                self._pending.pop(sequence, None)
            raise ChildTransportError(f"child CodeLLDB write failed: {error}") from error
        return sequence

    def request(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> Message:
        sequence = self.send(command, arguments)
        return self._wait_for_request(sequence)

    def _wait_for_request(self, sequence: int) -> Message:
        with self._pending_lock:
            pending = self._pending.get(sequence)
        assert pending is not None
        if not pending.complete.wait(self._timeout):
            with self._pending_lock:
                self._pending.pop(sequence, None)
            raise ChildTransportError(
                f"child CodeLLDB {command!r} request timed out after "
                f"{self._timeout:.1f} seconds"
            )
        if pending.error is not None:
            raise ChildTransportError(str(pending.error)) from pending.error
        if pending.response is None:
            raise ChildTransportError(
                f"child CodeLLDB {command!r} completed without a response"
            )
        if pending.response.get("success") is not True:
            raise ChildTransportError(
                str(
                    pending.response.get(
                        "message",
                        f"child {pending.command} failed",
                    )
                )
            )
        return pending.response

    def close(self) -> None:
        self._stop.set()
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def _wait_for_event(self, event: str) -> None:
        # The reader has already forwarded the event. This bounded queue avoids
        # racing startup setup against CodeLLDB's initialized notification.
        while True:
            try:
                message = self._startup_events.get(timeout=self._timeout)
            except Empty as error:
                raise ChildTransportError(
                    f"child CodeLLDB did not emit {event!r}"
                ) from error
            if message.get("event") == event:
                return

    def _reader_loop(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        reader = DapReader(process.stdout)
        try:
            while not self._stop.is_set():
                message = reader.read_message()
                if message is None:
                    self._fail_pending(ChildTransportError("child CodeLLDB closed DAP output"))
                    return
                if message.get("type") == "response":
                    sequence = message.get("request_seq")
                    if isinstance(sequence, int):
                        with self._pending_lock:
                            pending = self._pending.pop(sequence, None)
                        if pending is not None:
                            pending.response = message
                            pending.complete.set()
                    continue
                if message.get("type") == "event":
                    self._startup_events.put(message)
                    self._event_handler(message)
        except (DapProtocolError, OSError, ValueError) as error:
            self._fail_pending(error)
        finally:
            self._stop.set()

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for request in pending:
            request.error = error
            request.complete.set()


def read_child_registry(path: Path) -> tuple[dict[str, Any], ...]:
    """Read valid JSON child registrations from a directory without guessing."""

    if not path.is_dir():
        return ()
    records: list[dict[str, Any]] = []
    for candidate in sorted(path.glob("child-*.json")):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("pid"), int)
            and payload["pid"] > 0
            and isinstance(payload.get("parentPid"), int)
            and payload["parentPid"] > 0
        ):
            records.append(payload)
    return tuple(records)
