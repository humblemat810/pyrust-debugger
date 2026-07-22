"""Multiprocess child registration and DAP identity virtualization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock, Thread
import time
from typing import Any, Callable, Mapping, Sequence

from .child_transport import ChildCodelldbTransport, ChildTransportError, read_child_registry
from .state import ProxySessionState


Message = dict[str, Any]
EventCallback = Callable[[str, Mapping[str, Any]], None]
TransportFactory = Callable[
    [Sequence[str], str, Callable[[Message], None]],
    ChildCodelldbTransport,
]

_FRAME_ID_START = 1_000_000_000
_VARIABLE_REFERENCE_START = 1_500_000_000


@dataclass
class _ChildSession:
    process_id: int
    transport: ChildCodelldbTransport
    threads: dict[int, str | None]
    ready: bool = False


@dataclass(frozen=True)
class _FrameRoute:
    process_id: int
    thread_id: int
    frame_id: int


@dataclass(frozen=True)
class _VariableRoute:
    process_id: int
    thread_id: int
    variables_reference: int


class ProcessManager:
    """Attach one CodeLLDB transport to each explicitly registered child."""

    def __init__(
        self,
        *,
        registry_path: Path,
        adapter_command: Sequence[str],
        cwd: str,
        state: ProxySessionState,
        emit_event: EventCallback,
        force_single_thread: bool = False,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self._registry_path = registry_path
        self._adapter_command = list(adapter_command)
        self._cwd = cwd
        self._state = state
        self._emit_event = emit_event
        self._force_single_thread = force_single_thread
        self._transport_factory = transport_factory or _default_transport_factory
        self._lock = Lock()
        self._breakpoints: list[dict[str, Any]] = []
        self._configuration_done = False
        self._sessions: dict[int, _ChildSession] = {}
        self._retired_process_ids: set[int] = set()
        self._frame_routes: dict[int, _FrameRoute] = {}
        self._frame_keys: dict[tuple[int, int, int], int] = {}
        self._variable_routes: dict[int, _VariableRoute] = {}
        self._variable_keys: dict[tuple[int, int, int], int] = {}
        self._next_frame_id = _FRAME_ID_START
        self._next_variable_reference = _VARIABLE_REFERENCE_START
        self._stop = False
        self._watcher = Thread(
            target=self._watch_registry,
            name="pyrust-child-registry",
            daemon=True,
        )
        self._watcher.start()

    def add_breakpoints(self, arguments: Mapping[str, Any]) -> None:
        source = arguments.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            return
        breakpoints = arguments.get("breakpoints")
        if not isinstance(breakpoints, list):
            return
        record = {
            "source": {"path": source["path"]},
            "breakpoints": [
                {"line": item["line"]}
                for item in breakpoints
                if isinstance(item, dict)
                and isinstance(item.get("line"), int)
                and item["line"] > 0
            ],
            "sourceModified": bool(arguments.get("sourceModified", False)),
        }
        if not record["breakpoints"]:
            return
        with self._lock:
            self._breakpoints = [
                existing
                for existing in self._breakpoints
                if existing["source"]["path"] != record["source"]["path"]
            ]
            self._breakpoints.append(record)

    def mark_configuration_done(self) -> None:
        with self._lock:
            self._configuration_done = True

    def owns_process(self, process_id: object) -> bool:
        return isinstance(process_id, int) and process_id in self._sessions

    def threads(self) -> list[dict[str, Any]]:
        self.refresh_threads()
        with self._lock:
            sessions = tuple(
                (session.process_id, dict(session.threads))
                for session in self._sessions.values()
            )
        threads: list[dict[str, Any]] = []
        for process_id, process_threads in sessions:
            for thread_id in sorted(process_threads):
                name = process_threads[thread_id]
                threads.append(
                    {
                        "id": thread_id,
                        "name": (
                            f"process {process_id}: {name}"
                            if name
                            else f"process {process_id}: tid={thread_id}"
                        ),
                    }
                )
        return threads

    def refresh_threads(self) -> None:
        """Refresh declared worker identities without blocking on CodeLLDB."""

        with self._lock:
            sessions = tuple(self._sessions.values())
        records = {
            record["pid"]: record for record in read_child_registry(self._registry_path)
        }
        for session in sessions:
            record = records.get(session.process_id)
            if record is None:
                continue
            declared_threads = record.get("threads")
            if not isinstance(declared_threads, list):
                continue
            for thread in declared_threads:
                if not isinstance(thread, dict):
                    continue
                thread_id = thread.get("threadId")
                if (
                    not isinstance(thread_id, int)
                    or isinstance(thread_id, bool)
                    or thread_id <= 0
                ):
                    continue
                name = thread.get("name")
                self._record_thread(
                    session.process_id,
                    thread_id,
                    name=name if isinstance(name, str) and name else None,
                    activate=False,
                )

    def owns_thread(self, thread_id: object) -> bool:
        return isinstance(thread_id, int) and self._session_for_thread(thread_id) is not None

    def stack_trace(
        self,
        thread_id: int,
        arguments: Mapping[str, Any],
    ) -> Message:
        session = self._session_for_thread(thread_id)
        if session is None:
            raise ChildTransportError(f"no child transport owns thread {thread_id}")
        child_arguments: dict[str, Any] = {
            "threadId": thread_id,
            "startFrame": 0,
        }
        levels = arguments.get("levels")
        if isinstance(levels, int) and levels > 0:
            child_arguments["levels"] = levels
        stack_format = arguments.get("format")
        if isinstance(stack_format, dict):
            child_arguments["format"] = dict(stack_format)
        response = session.transport.request(
            "stackTrace",
            child_arguments,
        )
        body = dict(response.get("body") or {})
        frames = body.get("stackFrames")
        if not isinstance(frames, list):
            raise ChildTransportError("child stackTrace response has no stackFrames")
        translated = [
            self._translate_frame(session.process_id, thread_id, frame)
            for frame in frames
        ]
        return {
            "success": True,
            "body": {"stackFrames": translated, "totalFrames": len(translated)},
        }

    def native_frame_route(self, frame_id: object) -> _FrameRoute | None:
        return self._frame_routes.get(frame_id) if isinstance(frame_id, int) else None

    def variable_route(self, reference: object) -> _VariableRoute | None:
        return (
            self._variable_routes.get(reference)
            if isinstance(reference, int)
            else None
        )

    def scopes(self, frame_id: int) -> Message:
        route = self._require_frame(frame_id)
        session = self._require_session(route.process_id)
        response = session.transport.request("scopes", {"frameId": route.frame_id})
        body = dict(response.get("body") or {})
        scopes = body.get("scopes")
        if not isinstance(scopes, list):
            raise ChildTransportError("child scopes response is malformed")
        return {
            "success": True,
            "body": {
                "scopes": [
                    self._translate_variables_reference(
                        route.process_id,
                        route.thread_id,
                        scope,
                    )
                    for scope in scopes
                    if isinstance(scope, dict)
                ]
            },
        }

    def variables(self, reference: int) -> Message:
        route = self._require_variable(reference)
        session = self._require_session(route.process_id)
        response = session.transport.request(
            "variables",
            {"variablesReference": route.variables_reference},
        )
        body = dict(response.get("body") or {})
        variables = body.get("variables")
        if not isinstance(variables, list):
            raise ChildTransportError("child variables response is malformed")
        return {
            "success": True,
            "body": {
                "variables": [
                    self._translate_variables_reference(
                        route.process_id,
                        route.thread_id,
                        variable,
                    )
                    for variable in variables
                    if isinstance(variable, dict)
                ]
            },
        }

    def evaluate(self, frame_id: int, arguments: Mapping[str, Any]) -> Message:
        route = self._require_frame(frame_id)
        session = self._require_session(route.process_id)
        response = session.transport.request(
            "evaluate",
            {
                "expression": arguments.get("expression", ""),
                "frameId": route.frame_id,
                "context": arguments.get("context", "watch"),
            },
        )
        body = response.get("body")
        if not isinstance(body, dict):
            return {"success": True, "body": {}}
        return {
            "success": True,
            "body": self._translate_variables_reference(
                route.process_id,
                route.thread_id,
                body,
            ),
        }

    def set_variable(
        self,
        reference: int,
        arguments: Mapping[str, Any],
    ) -> Message:
        route = self._require_variable(reference)
        response = self._require_session(route.process_id).transport.request(
            "setVariable",
            {
                "variablesReference": route.variables_reference,
                "name": arguments.get("name", ""),
                "value": arguments.get("value", ""),
            },
        )
        body = response.get("body")
        return {
            "success": True,
            "body": (
                self._translate_variables_reference(
                    route.process_id,
                    route.thread_id,
                    body,
                )
                if isinstance(body, dict)
                else {}
            ),
        }

    def set_expression(
        self,
        frame_id: int,
        arguments: Mapping[str, Any],
    ) -> Message:
        route = self._require_frame(frame_id)
        response = self._require_session(route.process_id).transport.request(
            "setExpression",
            {
                "expression": arguments.get("expression", ""),
                "value": arguments.get("value", ""),
                "frameId": route.frame_id,
            },
        )
        body = response.get("body")
        return {
            "success": True,
            "body": (
                self._translate_variables_reference(
                    route.process_id,
                    route.thread_id,
                    body,
                )
                if isinstance(body, dict)
                else {}
            ),
        }

    def continue_thread(self, thread_id: int, *, single_thread: bool = False) -> Message:
        return self._resume_thread(
            "continue",
            thread_id,
            {"singleThread": single_thread or self._force_single_thread},
        )

    def step_thread(
        self,
        command: str,
        thread_id: int,
        arguments: Mapping[str, Any],
    ) -> Message:
        if command not in {"next", "stepIn", "stepOut"}:
            raise ChildTransportError(f"unsupported child step command {command!r}")
        forwarded = {
            key: value
            for key, value in arguments.items()
            if key in {"granularity", "singleThread", "targetId"}
        }
        if self._force_single_thread:
            forwarded["singleThread"] = True
        return self._resume_thread(command, thread_id, forwarded)

    def pause_thread(self, thread_id: int) -> Message:
        session = self._session_for_thread(thread_id)
        if session is None:
            raise ChildTransportError(f"no child transport owns thread {thread_id}")
        return session.transport.request(
            "pause",
            {"threadId": thread_id},
        )

    def set_instruction_breakpoints(
        self,
        process_id: int,
        breakpoints: Sequence[Mapping[str, Any]],
    ) -> Message:
        return self._require_session(process_id).transport.request(
            "setInstructionBreakpoints",
            {
                "breakpoints": [
                    dict(breakpoint)
                    for breakpoint in breakpoints
                ]
            },
        )

    def _resume_thread(
        self,
        command: str,
        thread_id: int,
        arguments: Mapping[str, Any],
    ) -> Message:
        session = self._session_for_thread(thread_id)
        if session is None:
            raise ChildTransportError(f"no child transport owns thread {thread_id}")
        forwarded = dict(arguments)
        forwarded["threadId"] = thread_id
        response = session.transport.request(command, forwarded)
        # CodeLLDB reports continued asynchronously. Invalidate proxy-owned
        # Python frames as soon as the native resume was accepted so a client
        # cannot query a stale child frame in that small event gap.
        self._state.on_continued(
            {
                "body": {
                    "threadId": thread_id,
                    "systemProcessId": session.process_id,
                }
            }
        )
        self._clear_thread_routes(session.process_id, thread_id)
        return response

    def close(self) -> None:
        with self._lock:
            self._stop = True
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.transport.close()

    def _watch_registry(self) -> None:
        while True:
            with self._lock:
                if self._stop:
                    return
                ready = self._configuration_done and bool(self._breakpoints)
            if ready:
                for record in read_child_registry(self._registry_path):
                    self._start_child(record)
            time.sleep(0.025)

    def _start_child(self, record: Mapping[str, Any]) -> None:
        process_id = record["pid"]
        parent_process_id = record["parentPid"]
        if not (self._registry_path / f"ready-{process_id}").is_file():
            return
        with self._lock:
            if (
                process_id in self._sessions
                or process_id in self._retired_process_ids
                or self._stop
            ):
                return
            breakpoints = tuple(self._breakpoints)
        startup_events: list[Message] = []

        def handle_startup_event(event: Message) -> None:
            with self._lock:
                session = self._sessions.get(process_id)
                if session is None or not session.ready:
                    startup_events.append(event)
                    return
            self._handle_child_event(process_id, event)

        transport = self._transport_factory(
            self._adapter_command,
            self._cwd,
            handle_startup_event,
        )
        try:
            transport.start(process_id=process_id, breakpoints=breakpoints)
            self._state.register_process(
                process_id,
                parent_process_id=parent_process_id,
                display_name=str(record.get("label") or f"Child {process_id}"),
                role=(
                    str(record["role"])
                    if isinstance(record.get("role"), str) and record["role"]
                    else "Python child process"
                ),
                command=(
                    str(record["command"])
                    if isinstance(record.get("command"), str)
                    else None
                ),
                inherit_default_metadata=False,
            )
            session = _ChildSession(process_id, transport, {}, ready=True)
            with self._lock:
                if (
                    process_id in self._sessions
                    or process_id in self._retired_process_ids
                    or self._stop
                ):
                    transport.close()
                    return
                self._sessions[process_id] = session
                session.ready = True
            for event in startup_events:
                self._handle_child_event(process_id, event)
            with self._lock:
                session_is_live = process_id in self._sessions
            if not session_is_live:
                return
            # Do not issue a threads request before the child is released.
            # CodeLLDB's attach handshake remains asynchronous at this point;
            # the first real stopped/thread event supplies the authoritative ID.
            (self._registry_path / f"attached-{process_id}").touch()
        except Exception as error:
            transport.close()
            with self._lock:
                self._sessions.pop(process_id, None)
            self._state.remove_process(process_id)
            self._emit_event(
                "output",
                {
                    "category": "stderr",
                    "output": (
                        f"PyRust child process {process_id} attach failed: {error}\n"
                    ),
                },
            )

    def _handle_child_event(self, process_id: int, event: Message) -> None:
        name = event.get("event")
        body = dict(event.get("body") or {})
        thread_id = body.get("threadId")
        if (
            isinstance(thread_id, int)
            and not isinstance(thread_id, bool)
            and thread_id > 0
        ):
            self._record_thread(
                process_id,
                thread_id,
                name=(
                    body.get("threadName")
                    if isinstance(body.get("threadName"), str)
                    else None
                ),
            )
        body["systemProcessId"] = process_id
        if name == "stopped":
            self._state.on_stopped({"body": body})
        elif name == "continued":
            self._state.on_continued({"body": body})
            self._clear_process_routes(process_id)
        elif name in {"exited", "terminated"}:
            self._state.on_terminated({"body": body})
            self._clear_process_routes(process_id)
            with self._lock:
                exited_session = self._sessions.pop(process_id, None)
                self._retired_process_ids.add(process_id)
            if exited_session is not None:
                exited_session.transport.close()
        if name in {"stopped", "continued", "exited", "terminated", "thread"}:
            self._emit_event(str(name), body)

    def _record_thread(
        self,
        process_id: int,
        thread_id: int,
        *,
        name: str | None = None,
        activate: bool = True,
    ) -> None:
        with self._lock:
            session = self._sessions.get(process_id)
            if session is None:
                return
            existing_name = session.threads.get(thread_id)
            session.threads[thread_id] = name or existing_name
        self._state.bind_thread(
            process_id,
            thread_id,
            name=name,
            activate=activate,
        )

    def _translate_frame(
        self,
        process_id: int,
        thread_id: int,
        frame: Mapping[str, Any],
    ) -> dict[str, Any]:
        translated = dict(frame)
        frame_id = frame.get("id")
        if not isinstance(frame_id, int):
            raise ChildTransportError("child stack frame has no integer ID")
        translated["id"] = self._allocate_frame_id(process_id, thread_id, frame_id)
        return translated

    def _translate_variables_reference(
        self,
        process_id: int,
        thread_id: int,
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        translated = dict(value)
        reference = translated.get("variablesReference")
        if isinstance(reference, int) and reference > 0:
            translated["variablesReference"] = self._allocate_variable_reference(
                process_id,
                thread_id,
                reference,
            )
        return translated

    def _allocate_frame_id(self, process_id: int, thread_id: int, frame_id: int) -> int:
        key = (process_id, thread_id, frame_id)
        with self._lock:
            existing = self._frame_keys.get(key)
            if existing is not None:
                return existing
            result = self._next_frame_id
            self._next_frame_id += 1
            self._frame_keys[key] = result
            self._frame_routes[result] = _FrameRoute(process_id, thread_id, frame_id)
            return result

    def _allocate_variable_reference(
        self,
        process_id: int,
        thread_id: int,
        reference: int,
    ) -> int:
        key = (process_id, thread_id, reference)
        with self._lock:
            existing = self._variable_keys.get(key)
            if existing is not None:
                return existing
            result = self._next_variable_reference
            self._next_variable_reference += 1
            self._variable_keys[key] = result
            self._variable_routes[result] = _VariableRoute(
                process_id,
                thread_id,
                reference,
            )
            return result

    def _clear_process_routes(self, process_id: int) -> None:
        with self._lock:
            self._frame_routes = {
                virtual: route
                for virtual, route in self._frame_routes.items()
                if route.process_id != process_id
            }
            self._frame_keys = {
                key: virtual
                for key, virtual in self._frame_keys.items()
                if key[0] != process_id
            }
            self._variable_routes = {
                virtual: route
                for virtual, route in self._variable_routes.items()
                if route.process_id != process_id
            }
            self._variable_keys = {
                key: virtual
                for key, virtual in self._variable_keys.items()
                if key[0] != process_id
            }

    def _clear_thread_routes(self, process_id: int, thread_id: int) -> None:
        with self._lock:
            self._frame_routes = {
                virtual: route
                for virtual, route in self._frame_routes.items()
                if (route.process_id, route.thread_id) != (process_id, thread_id)
            }
            self._frame_keys = {
                key: virtual
                for key, virtual in self._frame_keys.items()
                if key[:2] != (process_id, thread_id)
            }
            self._variable_routes = {
                virtual: route
                for virtual, route in self._variable_routes.items()
                if (route.process_id, route.thread_id) != (process_id, thread_id)
            }
            self._variable_keys = {
                key: virtual
                for key, virtual in self._variable_keys.items()
                if key[:2] != (process_id, thread_id)
            }

    def _session_for_thread(self, thread_id: int) -> _ChildSession | None:
        with self._lock:
            for session in self._sessions.values():
                if thread_id in session.threads:
                    return session
        return None

    def _require_session(self, process_id: int) -> _ChildSession:
        with self._lock:
            session = self._sessions.get(process_id)
        if session is None:
            raise ChildTransportError(f"child process {process_id} is unavailable")
        return session

    def _require_frame(self, frame_id: int) -> _FrameRoute:
        route = self.native_frame_route(frame_id)
        if route is None:
            raise ChildTransportError(f"unknown child frame ID {frame_id}")
        return route

    def _require_variable(self, reference: int) -> _VariableRoute:
        route = self.variable_route(reference)
        if route is None:
            raise ChildTransportError(f"unknown child variables reference {reference}")
        return route


def _default_transport_factory(
    command: Sequence[str],
    cwd: str,
    event_handler: Callable[[Message], None],
) -> ChildCodelldbTransport:
    return ChildCodelldbTransport(command, cwd=cwd, event_handler=event_handler)
