"""Per-process debugpy coordination for Python-owned PyRust stops."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from threading import Lock, Thread
import time
from typing import Any, Callable, Mapping

from .python_transport import DebugpyTransport, PythonTransportError
from .state import ProxySessionState


Message = dict[str, Any]
EventCallback = Callable[[str, Mapping[str, Any]], None]
TransportFactory = Callable[
    [Callable[[Message], None]],
    DebugpyTransport,
]

_THREAD_ID_START = 1_600_000_000
_FRAME_ID_START = 1_700_000_000
_VARIABLE_REFERENCE_START = 1_800_000_000


@dataclass
class _PythonSession:
    process_id: int
    transport: DebugpyTransport
    threads: dict[int, str | None]
    ready: bool = False


@dataclass(frozen=True)
class _ThreadRoute:
    process_id: int
    thread_id: int


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


class PythonProcessManager:
    """Attach a private debugpy adapter to every registered Python process."""

    def __init__(
        self,
        *,
        registry_path: Path,
        state: ProxySessionState,
        emit_event: EventCallback,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self._registry_path = registry_path
        self._state = state
        self._emit_event = emit_event
        self._transport_factory = transport_factory or _default_transport_factory
        self._lock = Lock()
        self._breakpoints: list[dict[str, Any]] = []
        self._configuration_done = False
        self._sessions: dict[int, _PythonSession] = {}
        self._retired_process_ids: set[int] = set()
        self._thread_routes: dict[int, _ThreadRoute] = {}
        self._thread_keys: dict[tuple[int, int], int] = {}
        self._frame_routes: dict[int, _FrameRoute] = {}
        self._frame_keys: dict[tuple[int, int, int], int] = {}
        self._variable_routes: dict[int, _VariableRoute] = {}
        self._variable_keys: dict[tuple[int, int, int], int] = {}
        self._recent_frames: dict[int, list[dict[str, Any]]] = {}
        self._next_thread_id = _THREAD_ID_START
        self._next_frame_id = _FRAME_ID_START
        self._next_variable_reference = _VARIABLE_REFERENCE_START
        self._stop = False
        self._watcher = Thread(
            target=self._watch_registry,
            name="pyrust-debugpy-registry",
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

    def owns_thread(self, thread_id: object) -> bool:
        return isinstance(thread_id, int) and thread_id in self._thread_routes

    def has_python_stop(self) -> bool:
        with self._lock:
            process_ids = tuple(self._sessions)
        return any(
            self._state.coordinator.execution_owner(process_id) == "python"
            for process_id in process_ids
        )

    def threads(self) -> list[dict[str, Any]]:
        self.refresh_threads()
        with self._lock:
            routes = tuple(
                (virtual_id, route)
                for virtual_id, route in self._thread_routes.items()
            )
            sessions = dict(self._sessions)
        result: list[dict[str, Any]] = []
        for virtual_id, route in sorted(routes):
            session = sessions.get(route.process_id)
            if session is None:
                continue
            name = session.threads.get(route.thread_id)
            result.append(
                {
                    "id": virtual_id,
                    "name": (
                        f"process {route.process_id}: {name}"
                        if name
                        else f"process {route.process_id}: Python thread"
                    ),
                }
            )
        return result

    def refresh_threads(self) -> None:
        with self._lock:
            sessions = tuple(self._sessions.values())
        for session in sessions:
            try:
                response = session.transport.request("threads")
            except PythonTransportError:
                continue
            threads = (response.get("body") or {}).get("threads")
            if not isinstance(threads, list):
                continue
            for thread in threads:
                if not isinstance(thread, dict):
                    continue
                thread_id = thread.get("id")
                if not isinstance(thread_id, int) or isinstance(thread_id, bool):
                    continue
                name = thread.get("name")
                self._record_thread(
                    session.process_id,
                    thread_id,
                    name=name if isinstance(name, str) and name else None,
                )

    def stack_trace(
        self,
        thread_id: int,
        arguments: Mapping[str, Any],
    ) -> Message:
        route = self._require_thread(thread_id)
        session = self._require_session(route.process_id)
        child_arguments: dict[str, Any] = {
            "threadId": route.thread_id,
            "startFrame": 0,
        }
        levels = arguments.get("levels")
        if isinstance(levels, int) and levels > 0:
            child_arguments["levels"] = levels
        stack_format = arguments.get("format")
        if isinstance(stack_format, dict):
            child_arguments["format"] = dict(stack_format)
        response = session.transport.request("stackTrace", child_arguments)
        body = dict(response.get("body") or {})
        frames = body.get("stackFrames")
        if not isinstance(frames, list):
            raise PythonTransportError("debugpy stackTrace response has no stackFrames")
        translated = [
            self._translate_frame(route.process_id, route.thread_id, frame)
            for frame in frames
            if isinstance(frame, dict)
        ]
        self._record_recent_frames(route.process_id, translated)
        return {
            "success": True,
            "body": {"stackFrames": translated, "totalFrames": len(translated)},
        }

    def recent_frames(self, process_id: int) -> list[dict[str, Any]]:
        """Return the last debugpy-owned Python stack for one live process.

        The coordinator uses this only as a read-only fallback when CPython's
        external unwinder cannot read the immediately following Rust callback.
        """

        with self._lock:
            return [dict(frame) for frame in self._recent_frames.get(process_id, ())]

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
        response = self._require_session(route.process_id).transport.request(
            "scopes",
            {"frameId": route.frame_id},
        )
        body = dict(response.get("body") or {})
        scopes = body.get("scopes")
        if not isinstance(scopes, list):
            raise PythonTransportError("debugpy scopes response is malformed")
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
        response = self._require_session(route.process_id).transport.request(
            "variables",
            {"variablesReference": route.variables_reference},
        )
        body = dict(response.get("body") or {})
        variables = body.get("variables")
        if not isinstance(variables, list):
            raise PythonTransportError("debugpy variables response is malformed")
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
        response = self._require_session(route.process_id).transport.request(
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

    def continue_thread(self, thread_id: int, *, single_thread: bool = False) -> Message:
        route = self._require_thread(thread_id)
        self._require_session(route.process_id).transport.notify(
            "continue",
            {
                "threadId": route.thread_id,
                "singleThread": single_thread,
            },
        )
        body = {
            "threadId": thread_id,
            "allThreadsContinued": not single_thread,
        }
        # A Rust breakpoint may stop the target before debugpy answers this
        # request. The write itself is enough to release Python ownership.
        self._state.on_continued({"body": body}, owner="python")
        self._clear_process_routes(route.process_id)
        self._emit_event("continued", body)
        return {"success": True, "body": body}

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
                ready = self._configuration_done
            if ready:
                for record in read_debugpy_registry(self._registry_path):
                    self._start_process(record)
            time.sleep(0.025)

    def _start_process(self, record: Mapping[str, Any]) -> None:
        process_id = record["pid"]
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
            self._handle_event(process_id, event)

        transport = self._transport_factory(handle_startup_event)
        try:
            transport.start(
                host=record["host"],
                port=record["port"],
                breakpoints=breakpoints,
            )
            parent_process_id = record["parentPid"]
            if parent_process_id not in self._state.process_ids:
                parent_process_id = None
            self._state.register_process(
                process_id,
                parent_process_id=parent_process_id,
                display_name=(
                    str(record["label"])
                    if isinstance(record.get("label"), str) and record["label"]
                    else None
                ),
                role="Python process",
                command=(
                    str(record["command"])
                    if isinstance(record.get("command"), str)
                    else None
                ),
                engine="python",
            )
            session = _PythonSession(process_id, transport, {}, ready=True)
            with self._lock:
                if (
                    process_id in self._sessions
                    or process_id in self._retired_process_ids
                    or self._stop
                ):
                    transport.close()
                    return
                self._sessions[process_id] = session
            for event in startup_events:
                self._handle_event(process_id, event)
            self.refresh_threads()
            self._emit_event(
                "output",
                {
                    "category": "console",
                    "output": f"PyRust attached debugpy to Python process {process_id}\n",
                },
            )
        except Exception as error:
            transport.close()
            with self._lock:
                self._sessions.pop(process_id, None)
                self._retired_process_ids.add(process_id)
            self._write_attach_failure(process_id, error)
            self._emit_event(
                "output",
                {
                    "category": "stderr",
                    "output": (
                        f"PyRust debugpy attach failed for process {process_id}: "
                        f"{error}\n"
                    ),
                },
            )

    def _write_attach_failure(self, process_id: int, error: Exception) -> None:
        marker = self._registry_path / f"debugpy-{process_id}.failed"
        temporary = marker.with_name(f".{marker.name}.tmp")
        try:
            temporary.write_text(str(error), encoding="utf-8")
            temporary.replace(marker)
        except OSError:
            temporary.unlink(missing_ok=True)

    def _handle_event(self, process_id: int, event: Message) -> None:
        name = event.get("event")
        body = dict(event.get("body") or {})
        raw_thread_id = body.get("threadId")
        virtual_thread_id: int | None = None
        if (
            isinstance(raw_thread_id, int)
            and not isinstance(raw_thread_id, bool)
            and raw_thread_id > 0
        ):
            virtual_thread_id = self._record_thread(
                process_id,
                raw_thread_id,
                name=(
                    body.get("threadName")
                    if isinstance(body.get("threadName"), str)
                    else None
                ),
            )
            body["threadId"] = virtual_thread_id
        body["systemProcessId"] = process_id
        was_python_owner = (
            self._state.coordinator.execution_owner(process_id) == "python"
        )
        if name == "stopped":
            self._state.on_stopped({"body": body}, owner="python")
        elif name == "continued":
            if was_python_owner:
                self._state.on_continued({"body": body}, owner="python")
            self._clear_process_routes(process_id)
        elif name in {"exited", "terminated"}:
            self._clear_process_routes(process_id)
            with self._lock:
                exited_session = self._sessions.pop(process_id, None)
                self._retired_process_ids.add(process_id)
                self._recent_frames.pop(process_id, None)
            if exited_session is not None:
                exited_session.transport.close()
            self._state.remove_process(process_id)
            return
        if name in {"stopped", "thread", "output", "breakpoint"}:
            self._emit_event(str(name), body)
        elif (
            name == "continued"
            and was_python_owner
        ):
            self._emit_event("continued", body)

    def _record_thread(
        self,
        process_id: int,
        thread_id: int,
        *,
        name: str | None = None,
    ) -> int:
        virtual_thread_id = self._allocate_thread_id(process_id, thread_id)
        with self._lock:
            session = self._sessions.get(process_id)
            if session is not None:
                existing_name = session.threads.get(thread_id)
                session.threads[thread_id] = name or existing_name
        self._state.bind_python_thread(
            process_id,
            virtual_thread_id,
            name=name,
        )
        return virtual_thread_id

    def _translate_frame(
        self,
        process_id: int,
        thread_id: int,
        frame: Mapping[str, Any],
    ) -> dict[str, Any]:
        translated = dict(frame)
        frame_id = frame.get("id")
        if not isinstance(frame_id, int):
            raise PythonTransportError("debugpy stack frame has no integer ID")
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

    def _allocate_thread_id(self, process_id: int, thread_id: int) -> int:
        key = (process_id, thread_id)
        with self._lock:
            existing = self._thread_keys.get(key)
            if existing is not None:
                return existing
            result = self._next_thread_id
            self._next_thread_id += 1
            self._thread_keys[key] = result
            self._thread_routes[result] = _ThreadRoute(process_id, thread_id)
            return result

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

    def _record_recent_frames(
        self,
        process_id: int,
        frames: list[dict[str, Any]],
    ) -> None:
        snapshot: list[dict[str, Any]] = []
        for frame in frames:
            source = frame.get("source")
            path = source.get("path") if isinstance(source, dict) else None
            line = frame.get("line")
            name = frame.get("name")
            if (
                isinstance(name, str)
                and name
                and isinstance(path, str)
                and path
                and isinstance(line, int)
                and not isinstance(line, bool)
                and line > 0
            ):
                snapshot.append({"name": name, "path": path, "line": line})
        if snapshot:
            with self._lock:
                self._recent_frames[process_id] = snapshot

    def _require_session(self, process_id: int) -> _PythonSession:
        with self._lock:
            session = self._sessions.get(process_id)
        if session is None:
            raise PythonTransportError(f"debugpy process {process_id} is unavailable")
        return session

    def _require_thread(self, thread_id: int) -> _ThreadRoute:
        with self._lock:
            route = self._thread_routes.get(thread_id)
        if route is None:
            raise PythonTransportError(f"unknown debugpy thread ID {thread_id}")
        return route

    def _require_frame(self, frame_id: int) -> _FrameRoute:
        route = self.native_frame_route(frame_id)
        if route is None:
            raise PythonTransportError(f"unknown debugpy frame ID {frame_id}")
        return route

    def _require_variable(self, reference: int) -> _VariableRoute:
        route = self.variable_route(reference)
        if route is None:
            raise PythonTransportError(
                f"unknown debugpy variables reference {reference}"
            )
        return route


def read_debugpy_registry(path: Path) -> tuple[dict[str, Any], ...]:
    """Read complete endpoint records written by the opt-in sitecustomize hook."""

    if not path.is_dir():
        return ()
    records: list[dict[str, Any]] = []
    for candidate in sorted(path.glob("debugpy-*.json")):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if (
            isinstance(payload.get("pid"), int)
            and payload["pid"] > 0
            and isinstance(payload.get("parentPid"), int)
            and payload["parentPid"] > 0
            and isinstance(payload.get("host"), str)
            and payload["host"]
            and isinstance(payload.get("port"), int)
            and 0 < payload["port"] < 65_536
        ):
            records.append(payload)
    return tuple(records)


def _default_transport_factory(
    event_handler: Callable[[Message], None],
) -> DebugpyTransport:
    return DebugpyTransport(event_handler=event_handler)
