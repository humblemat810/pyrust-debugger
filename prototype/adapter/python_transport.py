"""Private debugpy DAP transport for one registered Python process."""

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
import socket
from threading import Event, Lock, Thread
from typing import Any, Callable, Mapping

from .framing import DapProtocolError, DapReader, DapWriter


Message = dict[str, Any]
EventHandler = Callable[[Message], None]


class PythonTransportError(RuntimeError):
    """A private debugpy adapter session could not complete an operation."""


AsyncErrorHandler = Callable[[PythonTransportError], None]
AsyncCompleteHandler = Callable[[], None]


@dataclass
class _PendingRequest:
    command: str
    complete: Event
    response: Message | None = None
    error: BaseException | None = None
    async_error_handler: AsyncErrorHandler | None = None
    async_complete_handler: AsyncCompleteHandler | None = None


class DebugpyTransport:
    """Run one debugpy adapter and attach it to a debuggee-local server."""

    def __init__(
        self,
        *,
        event_handler: EventHandler,
        timeout: float = 10.0,
    ) -> None:
        self._event_handler = event_handler
        self._timeout = timeout
        self._socket: socket.socket | None = None
        self._stream: Any = None
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
        host: str,
        port: int,
        breakpoints: Sequence[Mapping[str, Any]],
    ) -> None:
        if self._socket is not None:
            return
        try:
            connection = socket.create_connection((host, port), timeout=self._timeout)
        except OSError as error:
            raise PythonTransportError(
                f"could not connect to debugpy at {host}:{port}: {error}"
            ) from error
        connection.settimeout(None)
        self._socket = connection
        self._stream = connection.makefile("rwb", buffering=0)
        self._writer = DapWriter(self._stream)
        self._reader = Thread(
            target=self._reader_loop,
            name=f"pyrust-debugpy-{port}",
            daemon=True,
        )
        self._reader.start()

        self.request(
            "initialize",
            {
                "clientID": "pyrust-python-coordinator",
                "adapterID": "python",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsRunInTerminalRequest": False,
            },
        )
        attach_sequence = self.send(
            "attach",
            {"justMyCode": False, "subProcess": False},
        )
        self._wait_for_event("initialized")
        for breakpoint in breakpoints:
            self.request("setBreakpoints", dict(breakpoint))
        self.request("configurationDone")
        self._wait_for_request(attach_sequence)

    def send(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> int:
        writer = self._writer
        if writer is None or self._stop.is_set():
            raise PythonTransportError("debugpy transport is unavailable")
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
            raise PythonTransportError(f"debugpy write failed: {error}") from error
        return sequence

    def request(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> Message:
        return self._wait_for_request(self.send(command, arguments))

    def notify(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        """Send a request whose late response is intentionally not awaited."""

        writer = self._writer
        if writer is None or self._stop.is_set():
            raise PythonTransportError("debugpy transport is unavailable")
        with self._sequence_lock:
            sequence = self._sequence
            self._sequence += 1
        request: Message = {"seq": sequence, "type": "request", "command": command}
        if arguments is not None:
            request["arguments"] = dict(arguments)
        try:
            writer.write_message(request)
        except OSError as error:
            raise PythonTransportError(f"debugpy write failed: {error}") from error

    def request_async(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        on_error: AsyncErrorHandler,
        on_complete: AsyncCompleteHandler | None = None,
    ) -> None:
        """Send a tracked request without blocking a second debug engine."""

        writer = self._writer
        if writer is None or self._stop.is_set():
            raise PythonTransportError("debugpy transport is unavailable")
        with self._sequence_lock:
            sequence = self._sequence
            self._sequence += 1
        pending = _PendingRequest(
            command=command,
            complete=Event(),
            async_error_handler=on_error,
            async_complete_handler=on_complete,
        )
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
            raise PythonTransportError(f"debugpy write failed: {error}") from error

    def close(self) -> None:
        self._stop.set()
        stream = self._stream
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        connection = self._socket
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def _wait_for_request(self, sequence: int) -> Message:
        with self._pending_lock:
            pending = self._pending.get(sequence)
        if pending is None:
            raise PythonTransportError(
                f"debugpy request {sequence} was not registered"
            )
        if not pending.complete.wait(self._timeout):
            with self._pending_lock:
                self._pending.pop(sequence, None)
            raise PythonTransportError(
                f"debugpy {pending.command!r} request timed out after "
                f"{self._timeout:.1f} seconds"
            )
        with self._pending_lock:
            self._pending.pop(sequence, None)
        if pending.error is not None:
            raise PythonTransportError(str(pending.error)) from pending.error
        if pending.response is None:
            raise PythonTransportError(
                f"debugpy {pending.command!r} completed without a response"
            )
        if pending.response.get("success") is not True:
            raise PythonTransportError(
                str(
                    pending.response.get(
                        "message",
                        f"debugpy {pending.command} failed",
                    )
                )
            )
        return pending.response

    def _wait_for_event(self, event: str) -> None:
        while True:
            try:
                message = self._startup_events.get(timeout=self._timeout)
            except Empty as error:
                raise PythonTransportError(
                    f"debugpy did not emit {event!r}"
                ) from error
            if message.get("event") == event:
                return

    def _reader_loop(self) -> None:
        stream = self._stream
        assert stream is not None
        reader = DapReader(stream)
        try:
            while not self._stop.is_set():
                message = reader.read_message()
                if message is None:
                    self._fail_pending(PythonTransportError("debugpy closed DAP output"))
                    return
                if message.get("type") == "response":
                    sequence = message.get("request_seq")
                    if isinstance(sequence, int):
                        with self._pending_lock:
                            pending = self._pending.get(sequence)
                        if pending is not None:
                            if pending.async_error_handler is not None:
                                with self._pending_lock:
                                    self._pending.pop(sequence, None)
                                if message.get("success") is not True:
                                    pending.async_error_handler(
                                        PythonTransportError(
                                            str(
                                                message.get(
                                                    "message",
                                                    f"debugpy {pending.command} failed",
                                                )
                                            )
                                        )
                                    )
                                elif pending.async_complete_handler is not None:
                                    pending.async_complete_handler()
                                continue
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
            if request.async_error_handler is not None:
                request.async_error_handler(PythonTransportError(str(error)))
                continue
            request.error = error
            request.complete.set()
