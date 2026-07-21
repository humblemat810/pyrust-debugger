"""Bidirectional stdio DAP proxy with narrow mixed-stack hook points."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import os
from queue import Empty, Queue
import select
import subprocess
import sys
from threading import Event, Lock, Thread
from typing import Any, BinaryIO, Callable, Mapping, Sequence

from .framing import DapProtocolError, DapReader, DapStreamParser, DapWriter
from .state import ProxySessionState, SyntheticFrameRegistry


Message = dict[str, Any]
LogFunction = Callable[[str], None]


class DownstreamRequestTimeout(TimeoutError):
    """Raised when an internal hook request does not receive a response."""


class DownstreamRequestError(RuntimeError):
    """Raised when the downstream transport cannot accept an internal request."""


@dataclass(frozen=True)
class LocalResponse:
    """A response produced by a proxy hook instead of CodeLLDB."""

    success: bool = True
    body: Mapping[str, Any] | None = None
    message: str | None = None


@dataclass
class _PendingInternalRequest:
    command: str
    completed: Event
    response: Message | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class _ForwardedClientRequest:
    request_seq: int
    command: str


@dataclass(frozen=True)
class _ForwardedDownstreamRequest:
    request_seq: int
    command: str


class ProxyHooks:
    """Override only the traffic needed by mixed-stack integration."""

    def on_launch(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> Message | LocalResponse:
        return request

    def on_stopped(self, event: Message, context: "ProxyContext") -> Message | None:
        return event

    def on_continued(
        self,
        event: Message,
        context: "ProxyContext",
    ) -> Message | None:
        return event

    def on_stack_trace(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> LocalResponse | None:
        return None

    def on_threads(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> LocalResponse | None:
        return None

    def on_continue_request(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> LocalResponse | None:
        return None

    def on_step_request(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> LocalResponse | None:
        return None

    def on_pause_request(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> LocalResponse | None:
        return None

    def on_restart(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> LocalResponse | None:
        return None

    def on_set_breakpoints(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> LocalResponse | None:
        return None

    def on_set_function_breakpoints(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> LocalResponse | None:
        return None

    def on_set_variable(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> LocalResponse | None:
        return None

    def on_set_expression(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> LocalResponse | None:
        return None

    def on_configuration_done(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> LocalResponse | None:
        return None

    def on_process_tree(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> LocalResponse | None:
        return None

    def close(self) -> None:
        """Release hook-owned resources after the DAP session ends."""

    def on_scopes(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> LocalResponse | None:
        frame_id = request.get("arguments", {}).get("frameId")
        if (
            isinstance(frame_id, int)
            and context.synthetic_frames.classify(frame_id) == "current"
        ):
            return LocalResponse(body={"scopes": []})
        return None

    def on_evaluate(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> LocalResponse | None:
        frame_id = request.get("arguments", {}).get("frameId")
        if (
            isinstance(frame_id, int)
            and context.synthetic_frames.classify(frame_id) == "current"
        ):
            return LocalResponse(
                success=False,
                message="evaluation is unavailable for synthetic Python frames",
            )
        return None

    def on_variables(
        self,
        request: Message,
        context: "ProxyContext",
    ) -> LocalResponse | None:
        reference = request.get("arguments", {}).get("variablesReference")
        if (
            isinstance(reference, int)
            and context.synthetic_frames.classify(reference) == "current"
        ):
            return LocalResponse(
                success=False,
                message="variables are unavailable for synthetic Python frames",
            )
        return None


class ProxyContext:
    """Services and stop state available to proxy hooks."""

    def __init__(self, proxy: "DapProxy", state: ProxySessionState) -> None:
        self._proxy = proxy
        self.state = state

    @property
    def synthetic_frames(self) -> SyntheticFrameRegistry:
        return self.state.synthetic_frames

    def request_downstream(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Message:
        return self._proxy.request_downstream(command, arguments, timeout=timeout)

    def send_event(
        self,
        event: str,
        body: Mapping[str, Any] | None = None,
    ) -> None:
        message: Message = {"type": "event", "event": event}
        if body is not None:
            message["body"] = dict(body)
        self._proxy.send_upstream(message)

    def send_output(self, output: str, *, category: str = "console") -> None:
        self.send_event("output", {"category": category, "output": output})


class DapProxy:
    """Relay one upstream DAP stream to a child downstream adapter."""

    _HOOK_COMMANDS = frozenset(
        {
            "configurationDone",
            "continue",
            "evaluate",
            "next",
            "pause",
            "restart",
            "scopes",
            "setBreakpoints",
            "setExpression",
            "setFunctionBreakpoints",
            "setVariable",
            "stackTrace",
            "stepIn",
            "stepOut",
            "threads",
            "variables",
            "pyrust/processTree",
        }
    )

    def __init__(
        self,
        downstream_command: Sequence[str],
        *,
        upstream_input: BinaryIO | None = None,
        upstream_output: BinaryIO | None = None,
        downstream_env: Mapping[str, str] | None = None,
        downstream_cwd: str | os.PathLike[str] | None = None,
        hooks: ProxyHooks | None = None,
        state: ProxySessionState | None = None,
        request_timeout: float = 5.0,
        shutdown_timeout: float = 2.0,
        log: LogFunction | None = None,
    ) -> None:
        if not downstream_command:
            raise ValueError("downstream command must not be empty")
        if request_timeout <= 0:
            raise ValueError("request timeout must be positive")
        if shutdown_timeout <= 0:
            raise ValueError("shutdown timeout must be positive")

        self._downstream_command = list(downstream_command)
        self._upstream_input = upstream_input or sys.stdin.buffer
        self._upstream_output = upstream_output or sys.stdout.buffer
        self._downstream_env = dict(downstream_env) if downstream_env is not None else None
        self._downstream_cwd = downstream_cwd
        self._hooks = hooks or ProxyHooks()
        self._request_timeout = request_timeout
        self._shutdown_timeout = shutdown_timeout
        self._log = log or (lambda line: print(line, file=sys.stderr, flush=True))

        self.state = state or ProxySessionState()
        self.context = ProxyContext(self, self.state)

        self._process: subprocess.Popen[bytes] | None = None
        self._upstream_writer = DapWriter(self._upstream_output)
        self._downstream_writer: DapWriter | None = None
        self._sequence_lock = Lock()
        self._next_upstream_seq = 1
        self._next_downstream_seq = 1
        self._pending_lock = Lock()
        self._forwarded_client_requests: dict[int, _ForwardedClientRequest] = {}
        self._forwarded_downstream_requests: dict[
            int, _ForwardedDownstreamRequest
        ] = {}
        self._pending_internal: dict[int, _PendingInternalRequest] = {}
        self._abandoned_internal: set[int] = set()

        self._stop = Event()
        self._upstream_eof = Event()
        self._downstream_eof = Event()
        self._disconnect_requested = Event()
        self._terminated_event_seen = Event()
        self._fatal_lock = Lock()
        self._fatal_message: str | None = None
        self._stderr_lines: deque[str] = deque(maxlen=40)
        self._workers: list[Thread] = []
        self._event_hook_queue: Queue[tuple[str, Message]] = Queue()

    @property
    def fatal_message(self) -> str | None:
        with self._fatal_lock:
            return self._fatal_message

    def run(self) -> int:
        try:
            self._start_downstream()
        except OSError as error:
            self._record_fatal(
                f"downstream startup failed for {self._downstream_command[0]!r}: {error}"
            )
            return 1

        process = self._require_process()
        self._start_worker(self._upstream_loop, "dap-upstream-reader")
        self._start_worker(self._downstream_loop, "dap-downstream-reader")
        self._start_worker(self._event_hook_loop, "dap-event-hooks")
        if process.stderr is not None:
            self._start_worker(self._stderr_loop, "dap-downstream-stderr")

        while not self._stop.wait(0.05):
            return_code = process.poll()
            if return_code is not None:
                # Let the stdout reader drain its final bytes before deciding
                # why the adapter stopped. This preserves framing errors that
                # would otherwise be hidden by the process-exit race.
                if not self._downstream_eof.is_set():
                    continue
                if not (
                    self._disconnect_requested.is_set()
                    or self._upstream_eof.is_set()
                    or self._terminated_event_seen.is_set()
                ):
                    detail = self._stderr_detail()
                    self._record_fatal(
                        f"downstream adapter exited unexpectedly with status "
                        f"{return_code}{detail}"
                    )
                self._stop.set()

        self._fail_pending(
            DownstreamRequestError(
                self.fatal_message or "downstream adapter connection closed"
            )
        )
        self._shutdown_downstream()
        self._hooks.close()
        return 1 if self.fatal_message else 0

    def request_downstream(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Message:
        if self._stop.is_set():
            raise DownstreamRequestError(
                self.fatal_message or "downstream adapter is not available"
            )

        sequence = self._allocate_downstream_sequence()
        pending = _PendingInternalRequest(command=command, completed=Event())
        with self._pending_lock:
            self._pending_internal[sequence] = pending

        request: Message = {
            "seq": sequence,
            "type": "request",
            "command": command,
        }
        if arguments is not None:
            request["arguments"] = dict(arguments)

        try:
            self._write_downstream(request)
        except BaseException:
            with self._pending_lock:
                self._pending_internal.pop(sequence, None)
            raise

        wait_timeout = self._request_timeout if timeout is None else timeout
        if wait_timeout <= 0:
            raise ValueError("downstream request timeout must be positive")
        if not pending.completed.wait(wait_timeout):
            with self._pending_lock:
                self._pending_internal.pop(sequence, None)
                self._abandoned_internal.add(sequence)
            raise DownstreamRequestTimeout(
                f"downstream {command!r} request timed out after "
                f"{wait_timeout:.3f} seconds"
            )
        if pending.error is not None:
            raise DownstreamRequestError(str(pending.error)) from pending.error
        if pending.response is None:
            raise DownstreamRequestError(
                f"downstream {command!r} request completed without a response"
            )
        return pending.response

    def send_upstream(self, message: Message) -> None:
        outgoing = dict(message)
        outgoing["seq"] = self._allocate_upstream_sequence()
        self._upstream_writer.write_message(outgoing)

    def _start_downstream(self) -> None:
        self._process = subprocess.Popen(
            self._downstream_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._downstream_cwd,
            env=self._downstream_env,
            bufsize=0,
        )
        if self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("downstream adapter pipes were not created")
        self._downstream_writer = DapWriter(self._process.stdin)

    def _start_worker(self, target: Callable[[], None], name: str) -> None:
        worker = Thread(target=target, name=name, daemon=True)
        self._workers.append(worker)
        worker.start()

    def _upstream_loop(self) -> None:
        try:
            try:
                file_descriptor = self._upstream_input.fileno()
            except (AttributeError, OSError):
                file_descriptor = None

            if file_descriptor is None:
                self._blocking_upstream_loop()
            else:
                self._selecting_upstream_loop(file_descriptor)
        except (DapProtocolError, OSError, ValueError) as error:
            self._record_fatal(f"upstream DAP protocol error: {error}")
        except Exception as error:
            self._record_fatal(f"upstream DAP relay failed: {error}")

    def _blocking_upstream_loop(self) -> None:
        reader = DapReader(self._upstream_input)
        while not self._stop.is_set():
            message = reader.read_message()
            if message is None:
                self._upstream_eof.set()
                self._stop.set()
                return
            self._handle_upstream_message(message)

    def _selecting_upstream_loop(self, file_descriptor: int) -> None:
        parser = DapStreamParser()
        while not self._stop.is_set():
            readable, _, _ = select.select([file_descriptor], [], [], 0.05)
            if not readable:
                continue
            chunk = os.read(file_descriptor, 64 * 1024)
            if not chunk:
                parser.feed_eof()
                self._upstream_eof.set()
                self._stop.set()
                return
            for message in parser.feed(chunk):
                self._handle_upstream_message(message)

    def _downstream_loop(self) -> None:
        process = self._require_process()
        assert process.stdout is not None
        reader = DapReader(process.stdout)
        try:
            while not self._stop.is_set():
                message = reader.read_message()
                if message is None:
                    self._downstream_eof.set()
                    if not (
                        self._disconnect_requested.is_set()
                        or self._upstream_eof.is_set()
                        or self._terminated_event_seen.is_set()
                    ):
                        self._record_fatal(
                            "downstream adapter closed its DAP output unexpectedly"
                            + self._stderr_detail()
                        )
                    else:
                        self._stop.set()
                    return
                self._handle_downstream_message(message)
        except (DapProtocolError, OSError, ValueError) as error:
            self._record_fatal(f"downstream DAP protocol error: {error}")
        except Exception as error:
            self._record_fatal(f"downstream DAP relay failed: {error}")

    def _stderr_loop(self) -> None:
        process = self._require_process()
        assert process.stderr is not None
        try:
            for raw_line in iter(process.stderr.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._stderr_lines.append(line)
                    self._log(f"[CodeLLDB] {line}")
        except OSError:
            return

    def _event_hook_loop(self) -> None:
        while not self._stop.is_set():
            try:
                event, message = self._event_hook_queue.get(timeout=0.05)
            except Empty:
                continue
            self._handle_event_hook(event, message)

    def _handle_upstream_message(self, message: Message) -> None:
        message_type = self._validate_message(message, "upstream")
        if message_type == "request":
            command = message.get("command")
            if command == "disconnect":
                self._disconnect_requested.set()
            if command == "launch":
                self._handle_launch_request(message)
                return
            if command in self._HOOK_COMMANDS:
                self._start_worker(
                    lambda: self._handle_hook_request(message),
                    f"dap-hook-{command}",
                )
            else:
                self._forward_client_request(message)
            return
        if message_type == "response":
            self._forward_client_response(message)
            return
        self._forward_to_downstream(message)

    def _handle_launch_request(self, request: Message) -> None:
        try:
            outgoing = self._hooks.on_launch(request, self.context)
            if isinstance(outgoing, LocalResponse):
                self._send_local_response(request, outgoing)
                return
            if not isinstance(outgoing, dict):
                raise TypeError("launch hook must return a DAP request object")
            if outgoing.get("command") != "launch":
                raise ValueError("launch hook must preserve the launch command")
            outgoing["seq"] = request["seq"]
            self._forward_client_request(outgoing)
        except BaseException as error:
            self._send_local_response(
                request,
                LocalResponse(
                    success=False,
                    message=f"PyRust launch hook failed: {error}",
                ),
            )

    def _handle_downstream_message(self, message: Message) -> None:
        message_type = self._validate_message(message, "downstream")
        if message_type == "response":
            self._handle_downstream_response(message)
            return
        if message_type == "request":
            self._forward_downstream_request(message)
            return

        event = message.get("event")
        if event == "process":
            self.state.record_process_event(message)
        elif event == "output":
            self.state.record_output_event(message)
        elif event == "stopped":
            self.state.on_stopped(message)
            self._event_hook_queue.put(("stopped", message))
            return
        elif event == "continued":
            self.state.on_continued(message)
            self._event_hook_queue.put(("continued", message))
            return
        elif event == "terminated":
            self._terminated_event_seen.set()
            self.state.on_terminated(message)
        self._forward_to_upstream(message)

    def _handle_hook_request(self, request: Message) -> None:
        command = request["command"]
        try:
            built_in = self._built_in_frame_response(request)
            if built_in is not None:
                response = built_in
            elif command == "stackTrace":
                response = self._hooks.on_stack_trace(request, self.context)
            elif command == "threads":
                response = self._hooks.on_threads(request, self.context)
            elif command == "continue":
                response = self._hooks.on_continue_request(request, self.context)
            elif command in {"next", "stepIn", "stepOut"}:
                response = self._hooks.on_step_request(request, self.context)
            elif command == "pause":
                response = self._hooks.on_pause_request(request, self.context)
            elif command == "restart":
                response = self._hooks.on_restart(request, self.context)
            elif command == "setBreakpoints":
                response = self._hooks.on_set_breakpoints(request, self.context)
            elif command == "setFunctionBreakpoints":
                response = self._hooks.on_set_function_breakpoints(
                    request,
                    self.context,
                )
            elif command == "setVariable":
                response = self._hooks.on_set_variable(request, self.context)
            elif command == "setExpression":
                response = self._hooks.on_set_expression(request, self.context)
            elif command == "configurationDone":
                response = self._hooks.on_configuration_done(request, self.context)
            elif command == "pyrust/processTree":
                response = self._hooks.on_process_tree(request, self.context)
            elif command == "scopes":
                response = self._hooks.on_scopes(request, self.context)
            elif command == "variables":
                response = self._hooks.on_variables(request, self.context)
            else:
                response = self._hooks.on_evaluate(request, self.context)

            if response is None:
                self._forward_client_request(request)
            else:
                self._send_local_response(request, response)
        except BaseException as error:
            self._send_local_response(
                request,
                LocalResponse(
                    success=False,
                    message=f"PyRust {command} hook failed: {error}",
                ),
            )

    def _built_in_frame_response(self, request: Message) -> LocalResponse | None:
        command = request["command"]
        if command not in {"scopes", "variables", "evaluate"}:
            return None
        arguments = request.get("arguments", {})
        frame_id = (
            arguments.get("variablesReference")
            if command == "variables"
            else arguments.get("frameId")
        )
        if not isinstance(frame_id, int):
            return None

        classification = self.state.synthetic_frames.classify(frame_id)
        if classification == "native":
            return None
        if classification == "stale":
            return LocalResponse(
                success=False,
                message="synthetic Python frame is no longer valid for this stop",
            )
        return None

    def _handle_event_hook(self, event: str, message: Message) -> None:
        try:
            if event == "stopped":
                outgoing = self._hooks.on_stopped(message, self.context)
            else:
                outgoing = self._hooks.on_continued(message, self.context)
            if outgoing is not None:
                self._forward_to_upstream(outgoing)
        except BaseException as error:
            self._record_fatal(f"PyRust {event} hook failed: {error}")

    def _forward_client_request(self, message: Message) -> None:
        original_sequence = self._require_int(message, "seq", "upstream request")
        command = self._require_string(message, "command", "upstream request")
        downstream_sequence = self._allocate_downstream_sequence()
        with self._pending_lock:
            self._forwarded_client_requests[downstream_sequence] = (
                _ForwardedClientRequest(original_sequence, command)
            )
        outgoing = dict(message)
        outgoing["seq"] = downstream_sequence
        try:
            self._write_downstream(outgoing)
        except BaseException:
            with self._pending_lock:
                self._forwarded_client_requests.pop(downstream_sequence, None)
            raise

    def _forward_client_response(self, message: Message) -> None:
        proxy_request_sequence = self._require_int(
            message,
            "request_seq",
            "upstream response",
        )
        with self._pending_lock:
            forwarded = self._forwarded_downstream_requests.pop(
                proxy_request_sequence,
                None,
            )
        if forwarded is None:
            raise DapProtocolError(
                "upstream response refers to an unknown downstream request"
            )
        response_command = self._require_string(
            message,
            "command",
            "upstream response",
        )
        if response_command != forwarded.command:
            raise DapProtocolError(
                f"upstream response command {response_command!r} does not match "
                f"{forwarded.command!r}"
            )
        outgoing = dict(message)
        outgoing["seq"] = self._allocate_downstream_sequence()
        outgoing["request_seq"] = forwarded.request_seq
        outgoing["command"] = forwarded.command
        self._write_downstream(outgoing)

    def _forward_downstream_request(self, message: Message) -> None:
        original_sequence = self._require_int(message, "seq", "downstream request")
        command = self._require_string(message, "command", "downstream request")
        upstream_sequence = self._allocate_upstream_sequence()
        with self._pending_lock:
            self._forwarded_downstream_requests[upstream_sequence] = (
                _ForwardedDownstreamRequest(original_sequence, command)
            )
        outgoing = dict(message)
        outgoing["seq"] = upstream_sequence
        try:
            self._upstream_writer.write_message(outgoing)
        except BaseException:
            with self._pending_lock:
                self._forwarded_downstream_requests.pop(upstream_sequence, None)
            raise

    def _handle_downstream_response(self, message: Message) -> None:
        request_sequence = self._require_int(
            message,
            "request_seq",
            "downstream response",
        )
        with self._pending_lock:
            internal = self._pending_internal.pop(request_sequence, None)
            forwarded = self._forwarded_client_requests.pop(request_sequence, None)
            abandoned = request_sequence in self._abandoned_internal
            self._abandoned_internal.discard(request_sequence)

        if internal is not None:
            self._validate_response_command(message, internal.command, "downstream")
            internal.response = message
            internal.completed.set()
            return
        if abandoned:
            return
        if forwarded is None:
            raise DapProtocolError(
                "downstream response refers to an unknown proxy request"
            )
        self._validate_response_command(message, forwarded.command, "downstream")
        if forwarded.command == "threads":
            self.state.record_threads_response(message)

        outgoing = dict(message)
        outgoing["seq"] = self._allocate_upstream_sequence()
        outgoing["request_seq"] = forwarded.request_seq
        outgoing["command"] = forwarded.command
        self._upstream_writer.write_message(outgoing)

    def _forward_to_downstream(self, message: Message) -> None:
        outgoing = dict(message)
        outgoing["seq"] = self._allocate_downstream_sequence()
        self._write_downstream(outgoing)

    def _forward_to_upstream(self, message: Message) -> None:
        outgoing = dict(message)
        outgoing["seq"] = self._allocate_upstream_sequence()
        self._upstream_writer.write_message(outgoing)

    def _send_local_response(
        self,
        request: Message,
        response: LocalResponse,
    ) -> None:
        outgoing: Message = {
            "seq": self._allocate_upstream_sequence(),
            "type": "response",
            "request_seq": self._require_int(request, "seq", "upstream request"),
            "command": self._require_string(request, "command", "upstream request"),
            "success": response.success,
        }
        if response.body is not None:
            outgoing["body"] = dict(response.body)
        if response.message is not None:
            outgoing["message"] = response.message
        self._upstream_writer.write_message(outgoing)

    def _write_downstream(self, message: Message) -> None:
        if self._stop.is_set():
            raise DownstreamRequestError(
                self.fatal_message or "downstream adapter is not available"
            )
        writer = self._downstream_writer
        if writer is None:
            raise DownstreamRequestError("downstream adapter has not started")
        try:
            writer.write_message(message)
        except (BrokenPipeError, OSError) as error:
            self._record_fatal(f"downstream DAP write failed: {error}")
            raise DownstreamRequestError(str(error)) from error

    def _allocate_upstream_sequence(self) -> int:
        with self._sequence_lock:
            sequence = self._next_upstream_seq
            self._next_upstream_seq += 1
            return sequence

    def _allocate_downstream_sequence(self) -> int:
        with self._sequence_lock:
            sequence = self._next_downstream_seq
            self._next_downstream_seq += 1
            return sequence

    def _record_fatal(self, message: str) -> None:
        should_log = False
        with self._fatal_lock:
            if self._fatal_message is None:
                self._fatal_message = message
                should_log = True
        if should_log:
            self._log(f"PyRust DAP proxy: {message}")
        self._stop.set()

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            pending = list(self._pending_internal.values())
            self._pending_internal.clear()
        for request in pending:
            request.error = error
            request.completed.set()

    def _shutdown_downstream(self) -> None:
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            try:
                process.wait(timeout=self._shutdown_timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=self._shutdown_timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=self._shutdown_timeout)

    def _stderr_detail(self) -> str:
        if not self._stderr_lines:
            return ""
        return f"; stderr: {self._stderr_lines[-1]}"

    def _require_process(self) -> subprocess.Popen[bytes]:
        if self._process is None:
            raise RuntimeError("downstream adapter has not started")
        return self._process

    @staticmethod
    def _validate_message(message: Message, peer: str) -> str:
        message_type = message.get("type")
        if message_type not in {"request", "response", "event"}:
            raise DapProtocolError(
                f"{peer} message has invalid type {message_type!r}"
            )
        DapProxy._require_int(message, "seq", f"{peer} message")
        if message_type == "request":
            DapProxy._require_string(message, "command", f"{peer} request")
        elif message_type == "response":
            DapProxy._require_int(message, "request_seq", f"{peer} response")
            command = message.get("command")
            if command is not None and not isinstance(command, str):
                raise DapProtocolError(
                    f"{peer} response command must be a string when present"
                )
        else:
            DapProxy._require_string(message, "event", f"{peer} event")
        return message_type

    @staticmethod
    def _validate_response_command(
        message: Message,
        expected: str,
        peer: str,
    ) -> None:
        actual = message.get("command")
        if actual in {None, ""}:
            return
        if not isinstance(actual, str):
            raise DapProtocolError(
                f"{peer} response command must be a string when present"
            )
        if actual != expected:
            raise DapProtocolError(
                f"{peer} response command {actual!r} does not match {expected!r}"
            )

    @staticmethod
    def _require_int(message: Message, key: str, context: str) -> int:
        value = message.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise DapProtocolError(f"{context} requires integer {key!r}")
        return value

    @staticmethod
    def _require_string(message: Message, key: str, context: str) -> str:
        value = message.get(key)
        if not isinstance(value, str) or not value:
            raise DapProtocolError(f"{context} requires non-empty string {key!r}")
        return value
