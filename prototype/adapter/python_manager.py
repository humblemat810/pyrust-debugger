"""Per-process debugpy coordination for Python-owned PyRust stops."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
import os
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
import time
from typing import Any, Callable, Mapping

from .python_transport import DebugpyTransport, PythonTransportError
from .state import ProxySessionState
from prototype.python.pyrust_stack.remote_debug import (
    queue_remote_debug_script,
    RemoteDebugError,
)


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
    native_threads: dict[int, int]
    resume_ready: Event
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


@dataclass(frozen=True)
class _HandoffStep:
    command: str
    thread_id: int
    target_name: str
    target_path: str
    target_line: int
    breakpoint_record: dict[str, Any]


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
        self._handoff_script = registry_path / "pyrust-debugpy-handoff.py"
        registry_path.mkdir(parents=True, exist_ok=True)
        self._handoff_script.write_text(
            "import os as _pyrust_os\n"
            "from pathlib import Path as _PyRustPath\n"
            "import time as _pyrust_time\n"
            "import debugpy as _pyrust_debugpy\n"
            f"_pyrust_registry = _PyRustPath({str(registry_path)!r})\n"
            "_pyrust_entered = _pyrust_registry / "
            "f'handoff-entered-{_pyrust_os.getpid()}'\n"
            "_pyrust_ready = _pyrust_registry / "
            "f'handoff-ready-{_pyrust_os.getpid()}'\n"
            "_pyrust_release = _pyrust_registry / "
            "f'handoff-release-{_pyrust_os.getpid()}'\n"
            "_pyrust_entered.touch()\n"
            "_pyrust_deadline = _pyrust_time.monotonic() + 5.0\n"
            "while not _pyrust_ready.exists() and "
            "_pyrust_time.monotonic() < _pyrust_deadline:\n"
            "    _pyrust_time.sleep(0.005)\n"
            "if not _pyrust_release.exists():\n"
            "    _pyrust_debugpy.breakpoint()\n"
            "_pyrust_deadline = _pyrust_time.monotonic() + 30.0\n"
            "while not _pyrust_release.exists() and "
            "_pyrust_time.monotonic() < _pyrust_deadline:\n"
            "    _pyrust_time.sleep(0.005)\n"
            "_pyrust_release.unlink(missing_ok=True)\n",
            encoding="utf-8",
        )
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
        self._handoff_targets: dict[int, tuple[str, str]] = {}
        self._handoff_exposed: set[int] = set()
        self._handoff_user_resuming: set[int] = set()
        self._handoff_resolving: set[int] = set()
        self._handoff_pending_stops: dict[int, dict[str, Any]] = {}
        self._handoff_entry_breakpoints: dict[int, _HandoffStep] = {}
        self._handoff_completed_targets: set[tuple[int, str, str]] = set()
        self._handoff_steps: dict[int, _HandoffStep] = {}
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
        with self._lock:
            self._breakpoints = [
                existing
                for existing in self._breakpoints
                if existing["source"]["path"] != record["source"]["path"]
            ]
            self._breakpoints.append(record)
            sessions = tuple(self._sessions.values())
        for session in sessions:
            try:
                session.transport.request("setBreakpoints", record)
            except PythonTransportError as error:
                self._emit_event(
                    "output",
                    {
                        "category": "stderr",
                        "output": (
                            "PyRust could not update debugpy breakpoints for "
                            f"process {session.process_id}: {error}\n"
                        ),
                    },
                )

    def mark_configuration_done(self) -> None:
        with self._lock:
            self._configuration_done = True

    def _all_breakpoints(self) -> list[dict[str, Any]]:
        return [dict(record) for record in self._breakpoints]

    def _breakpoint_record_with_temporary_line(
        self,
        path: str,
        line: int,
    ) -> dict[str, Any]:
        with self._lock:
            existing = next(
                (
                    record
                    for record in self._breakpoints
                    if record["source"]["path"] == path
                ),
                None,
            )
        breakpoints = [
            dict(item)
            for item in (existing or {}).get("breakpoints", ())
            if isinstance(item, dict)
        ]
        if not any(item.get("line") == line for item in breakpoints):
            breakpoints.append({"line": line})
        return {
            "source": {"path": path},
            "breakpoints": breakpoints,
            "sourceModified": bool((existing or {}).get("sourceModified", False)),
        }

    def _restore_handoff_breakpoint(self, process_id: int, step: _HandoffStep) -> None:
        session = self._require_session(process_id)
        with self._lock:
            existing = next(
                (
                    record
                    for record in self._breakpoints
                    if record["source"]["path"] == step.target_path
                ),
                None,
            )
        record = (
            dict(existing)
            if existing is not None
            else {
                "source": {"path": step.target_path},
                "breakpoints": [],
                "sourceModified": False,
            }
        )
        session.transport.request("setBreakpoints", record)

    def prepare_restart(self) -> None:
        """Retire old debugpy identities before CodeLLDB launches a new PID."""

        with self._lock:
            sessions = tuple(self._sessions.values())
            process_ids = tuple(self._sessions)
            self._sessions.clear()
            self._retired_process_ids.update(process_ids)
            self._thread_routes.clear()
            self._thread_keys.clear()
            self._frame_routes.clear()
            self._frame_keys.clear()
            self._variable_routes.clear()
            self._variable_keys.clear()
            self._recent_frames.clear()
            self._handoff_targets.clear()
            self._handoff_exposed.clear()
            self._handoff_user_resuming.clear()
            self._handoff_resolving.clear()
            self._handoff_pending_stops.clear()
            self._handoff_entry_breakpoints.clear()
            self._handoff_completed_targets.clear()
            self._handoff_steps.clear()
        for session in sessions:
            session.transport.close()
        for process_id in process_ids:
            self._state.remove_process(process_id)

    def owns_thread(self, thread_id: object) -> bool:
        return isinstance(thread_id, int) and thread_id in self._thread_routes

    def native_identity_for_thread(self, thread_id: int) -> tuple[int, int]:
        route = self._require_thread(thread_id)
        session = self._require_session(route.process_id)
        with self._lock:
            native_thread_id = next(
                (
                    native
                    for native, python in session.native_threads.items()
                    if python == route.thread_id
                ),
                None,
            )
        if native_thread_id is None:
            stack = session.transport.request(
                "stackTrace",
                {"threadId": route.thread_id, "startFrame": 0, "levels": 1},
            )
            frames = (stack.get("body") or {}).get("stackFrames")
            frame_id = (
                frames[0].get("id")
                if isinstance(frames, list)
                and frames
                and isinstance(frames[0], dict)
                else None
            )
            if not isinstance(frame_id, int):
                raise PythonTransportError(
                    "debugpy cannot resolve the selected thread's native ID"
                )
            evaluated = session.transport.request(
                "evaluate",
                {
                    "expression": "__import__('_thread').get_native_id()",
                    "frameId": frame_id,
                    "context": "watch",
                },
            )
            result = (evaluated.get("body") or {}).get("result")
            try:
                native_thread_id = int(str(result).strip())
            except (TypeError, ValueError) as error:
                raise PythonTransportError(
                    "debugpy returned an invalid native thread ID"
                ) from error
            if native_thread_id <= 0:
                raise PythonTransportError(
                    "debugpy returned an invalid native thread ID"
                )
            with self._lock:
                session.native_threads[native_thread_id] = route.thread_id
        return route.process_id, native_thread_id

    def is_handoff_exposed(self, thread_id: object) -> bool:
        if not isinstance(thread_id, int) or isinstance(thread_id, bool):
            return False
        with self._lock:
            route = self._thread_routes.get(thread_id)
            return route is not None and route.process_id in self._handoff_exposed

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
        with self._lock:
            handoff_target = self._handoff_targets.get(route.process_id)
        if handoff_target is not None:
            target_name, target_path = handoff_target
            target_index = next(
                (
                    index
                    for index, frame in enumerate(translated)
                    if frame.get("name") == target_name
                    and (frame.get("source") or {}).get("path") == target_path
                ),
                None,
            )
            if target_index is not None:
                translated = translated[target_index:]
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

    def direct_native_step_target(self, thread_id: int) -> str | None:
        """Return a conservative direct Rust call on the selected Python line."""

        route = self._require_thread(thread_id)
        with self._lock:
            frames = tuple(self._recent_frames.get(route.process_id, ()))
        if not frames:
            response = self._require_session(route.process_id).transport.request(
                "stackTrace",
                {
                    "threadId": route.thread_id,
                    "startFrame": 0,
                    "levels": 1,
                },
            )
            raw_frames = (response.get("body") or {}).get("stackFrames")
            if not isinstance(raw_frames, list) or not raw_frames:
                return None
            raw_top = raw_frames[0]
            if not isinstance(raw_top, dict):
                return None
            source = raw_top.get("source")
            frames = (
                {
                    "path": (
                        source.get("path")
                        if isinstance(source, dict)
                        else None
                    ),
                    "line": raw_top.get("line"),
                },
            )
        path = frames[0].get("path")
        line = frames[0].get("line")
        if not isinstance(path, str) or not isinstance(line, int) or line <= 0:
            return None
        try:
            source_line = Path(path).read_text(encoding="utf-8").splitlines()[line - 1]
            parsed = ast.parse(source_line.strip())
        except (OSError, IndexError, SyntaxError):
            return None
        calls = [node for node in ast.walk(parsed) if isinstance(node, ast.Call)]
        if len(calls) != 1:
            return None
        function = calls[0].func
        name = (
            function.id
            if isinstance(function, ast.Name)
            else function.attr
            if isinstance(function, ast.Attribute)
            else None
        )
        return name if isinstance(name, str) and name.startswith("rust_") else None

    def begin_handoff_step(self, thread_id: int, command: str) -> Message:
        """Run a transferred Python frame to a real Python source location.

        The CPython remote-debug helper sits above the user frame. Stepping it
        directly exposes helper internals, so `next` and `stepOut` use a
        temporary debugpy breakpoint at the requested user-level destination.
        """

        if command not in {"next", "stepOut"}:
            raise PythonTransportError(f"unsupported handoff step {command!r}")
        route = self._require_thread(thread_id)
        session = self._require_session(route.process_id)
        with self._lock:
            handoff = self._handoff_targets.get(route.process_id)
            exposed = route.process_id in self._handoff_exposed
        if handoff is None or not exposed:
            raise PythonTransportError("Python handoff is no longer active")
        target_name, target_path = handoff
        response = session.transport.request(
            "stackTrace",
            {"threadId": route.thread_id, "startFrame": 0, "levels": 200},
        )
        frames = (response.get("body") or {}).get("stackFrames")
        if not isinstance(frames, list):
            raise PythonTransportError("debugpy handoff stack is malformed")
        target_index = next(
            (
                index
                for index, frame in enumerate(frames)
                if isinstance(frame, dict)
                and frame.get("name") == target_name
                and isinstance(frame.get("source"), dict)
                and frame["source"].get("path") == target_path
            ),
            None,
        )
        if target_index is None:
            raise PythonTransportError("debugpy lost the selected Python handoff frame")
        selected_index = target_index if command == "next" else target_index + 1
        selected = frames[selected_index] if selected_index < len(frames) else None
        if not isinstance(selected, dict):
            raise PythonTransportError("Python stepOut has no caller frame")
        source = selected.get("source")
        path = source.get("path") if isinstance(source, dict) else None
        line = selected.get("line")
        name = selected.get("name")
        if (
            not isinstance(path, str)
            or not isinstance(line, int)
            or line <= 0
            or not isinstance(name, str)
            or not name
        ):
            raise PythonTransportError("Python handoff step has no source location")
        destination = _next_python_statement_line(path, line)
        if destination is None:
            raise PythonTransportError(
                f"Python {command} has no following executable source line"
            )
        record = self._breakpoint_record_with_temporary_line(path, destination)
        session.transport.request("setBreakpoints", record)
        step = _HandoffStep(command, route.thread_id, name, path, destination, record)
        with self._lock:
            self._handoff_steps[route.process_id] = step
            self._handoff_user_resuming.add(route.process_id)
        return self._resume_thread("continue", thread_id, {"singleThread": True})

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

    def set_variable(
        self,
        variables_reference: int,
        arguments: Mapping[str, Any],
    ) -> Message:
        route = self._require_variable(variables_reference)
        response = self._require_session(route.process_id).transport.request(
            "setVariable",
            {
                "variablesReference": route.variables_reference,
                "name": arguments.get("name", ""),
                "value": arguments.get("value", ""),
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
        return self._resume_thread(
            "continue",
            thread_id,
            {"singleThread": single_thread},
        )

    def step_thread(
        self,
        command: str,
        thread_id: int,
        arguments: Mapping[str, Any],
    ) -> Message:
        if command not in {"next", "stepIn", "stepOut"}:
            raise PythonTransportError(f"unsupported debugpy step command {command!r}")
        forwarded = {
            key: value
            for key, value in arguments.items()
            if key in {"granularity", "singleThread", "targetId"}
        }
        return self._resume_thread(command, thread_id, forwarded)

    def pause_thread(self, thread_id: int) -> Message:
        route = self._require_thread(thread_id)
        response = self._require_session(route.process_id).transport.request(
            "pause",
            {"threadId": route.thread_id},
        )
        return {"success": True, "body": dict(response.get("body") or {})}

    def arm_targeted_handoff(
        self,
        process_id: int,
        *,
        native_thread_id: int,
        target_name: str,
        target_path: str,
        target_line: int,
    ) -> None:
        """Queue a debugpy stop on the selected CPython native thread."""

        # Listener startup can lag behind process creation. This bounded wait
        # occurs outside the coordinator lock, so it does not block peers.
        deadline = time.monotonic() + 10
        while True:
            with self._lock:
                session = self._sessions.get(process_id)
            if session is not None:
                break
            if time.monotonic() >= deadline:
                raise PythonTransportError(
                    f"debugpy process {process_id} is unavailable"
                )
            time.sleep(0.025)
        target_key = (process_id, target_name, target_path)
        with self._lock:
            repeated_target = target_key in self._handoff_completed_targets
            self._handoff_targets[process_id] = (target_name, target_path)
            self._handoff_exposed.discard(process_id)
            self._handoff_user_resuming.discard(process_id)
            self._handoff_pending_stops.pop(process_id, None)
            self._handoff_entry_breakpoints.pop(process_id, None)
            self._handoff_steps.pop(process_id, None)
            debugpy_thread_id = session.native_threads.get(native_thread_id)
        destination = (
            _next_python_statement_line(target_path, target_line)
            if repeated_target
            else None
        )
        if destination is not None:
            record = self._breakpoint_record_with_temporary_line(
                target_path,
                destination,
            )
            with self._lock:
                self._handoff_entry_breakpoints[process_id] = _HandoffStep(
                    "handoff",
                    debugpy_thread_id or 0,
                    target_name,
                    target_path,
                    destination,
                    record,
                )
            session.transport.request_async(
                "setBreakpoints",
                record,
                on_error=lambda error: self._emit_event(
                    "output",
                    {
                        "category": "stderr",
                        "output": (
                            "PyRust could not arm the Python handoff fallback "
                            f"breakpoint for process {process_id}: {error}\n"
                        ),
                    },
                ),
            )
        ready_marker = self._registry_path / f"handoff-ready-{process_id}"
        entered_marker = self._registry_path / f"handoff-entered-{process_id}"
        release_marker = self._registry_path / f"handoff-release-{process_id}"
        ready_marker.unlink(missing_ok=True)
        entered_marker.unlink(missing_ok=True)
        release_marker.unlink(missing_ok=True)
        try:
            queue_remote_debug_script(
                process_id,
                native_thread_id,
                self._handoff_script,
                expected_name=target_name,
                expected_path=target_path,
                require_main_interpreter=True,
            )
        except RemoteDebugError as error:
            self._restore_entry_breakpoint(process_id)
            raise PythonTransportError(
                f"could not queue targeted CPython 3.14 handoff: {error}"
            ) from error
        Thread(
            target=self._wait_and_queue_handoff_pause,
            args=(
                session,
                entered_marker,
                ready_marker,
                debugpy_thread_id,
            ),
            name=f"pyrust-debugpy-handoff-ready-{process_id}",
            daemon=True,
        ).start()

    def _wait_and_queue_handoff_pause(
        self,
        session: _PythonSession,
        entered_marker: Path,
        ready_marker: Path,
        debugpy_thread_id: int | None,
    ) -> None:
        if not session.resume_ready.wait(10):
            self._emit_event(
                "output",
                {
                    "category": "stderr",
                    "output": (
                        "PyRust timed out waiting for the previous debugpy "
                        f"resume in process {session.process_id}\n"
                    ),
                },
            )
            ready_marker.touch()
            return
        deadline = time.monotonic() + 10
        while not entered_marker.is_file():
            if time.monotonic() >= deadline:
                self._emit_event(
                    "output",
                    {
                        "category": "stderr",
                        "output": (
                            "PyRust timed out waiting for selected Python "
                            f"thread {session.process_id} to enter handoff\n"
                        ),
                    },
                )
                ready_marker.touch()
                return
            time.sleep(0.005)
        self._queue_handoff_pause(
            session,
            ready_marker,
            debugpy_thread_id,
        )

    def _queue_handoff_pause(
        self,
        session: _PythonSession,
        ready_marker: Path,
        debugpy_thread_id: int | None,
    ) -> None:
        try:
            session.transport.request_async(
                "pause",
                {
                    "threadId": (
                        debugpy_thread_id
                        if debugpy_thread_id is not None
                        else "*"
                    )
                },
                on_error=lambda error: self._emit_event(
                    "output",
                    {
                        "category": "stderr",
                        "output": (
                            "PyRust debugpy handoff pause failed for process "
                            f"{session.process_id}: {error}\n"
                        ),
                    },
                ),
            )
            ready_marker.touch()
        except (OSError, PythonTransportError) as error:
            self._emit_event(
                "output",
                {
                    "category": "stderr",
                    "output": (
                        "PyRust could not queue the debugpy handoff pause for "
                        f"process {session.process_id}: {error}\n"
                    ),
                },
            )
            ready_marker.touch()

    def _resume_thread(
        self,
        command: str,
        thread_id: int,
        arguments: Mapping[str, Any],
    ) -> Message:
        route = self._require_thread(thread_id)
        session = self._require_session(route.process_id)
        with self._lock:
            if route.process_id in self._handoff_exposed:
                self._handoff_user_resuming.add(route.process_id)
        session.resume_ready.clear()
        forwarded = dict(arguments)
        forwarded["threadId"] = route.thread_id
        session.transport.request_async(
            command,
            forwarded,
            on_error=lambda error: self._on_resume_error(
                session,
                command,
                error,
            ),
            on_complete=session.resume_ready.set,
        )
        body = {
            "threadId": thread_id,
            "allThreadsContinued": not bool(forwarded.get("singleThread")),
        }
        # debugpy's continued event is the source of truth for releasing the
        # process lease and invalidating only the routes that actually resumed.
        return {"success": True, "body": body}

    def _on_resume_error(
        self,
        session: _PythonSession,
        command: str,
        error: Exception,
    ) -> None:
        session.resume_ready.set()
        self._emit_event(
            "output",
            {
                "category": "stderr",
                "output": (
                    f"PyRust debugpy {command} failed for process "
                    f"{session.process_id}: {error}\n"
                ),
            },
        )

    def close(self) -> None:
        with self._lock:
            self._stop = True
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
            self._handoff_targets.clear()
            self._handoff_exposed.clear()
            self._handoff_user_resuming.clear()
            self._handoff_resolving.clear()
            self._handoff_pending_stops.clear()
            self._handoff_entry_breakpoints.clear()
            self._handoff_completed_targets.clear()
            self._handoff_steps.clear()
        for session in sessions:
            session.transport.close()
        # The registry may be a short-lived launch directory.  Do not return
        # until its watcher has observed `_stop`, otherwise it can race the
        # caller's cleanup and recreate a marker in a removed directory.
        if self._watcher is not current_thread():
            self._watcher.join(timeout=1)

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
            breakpoints = tuple(self._all_breakpoints())
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
            native_threads = {
                item["nativeThreadId"]: item["pythonThreadId"]
                for item in record.get("threads", ())
                if isinstance(item, dict)
                and isinstance(item.get("nativeThreadId"), int)
                and item["nativeThreadId"] > 0
                and isinstance(item.get("pythonThreadId"), int)
                and item["pythonThreadId"] > 0
            }
            session = _PythonSession(
                process_id,
                transport,
                {},
                native_threads,
                Event(),
                ready=True,
            )
            session.resume_ready.set()
            with self._lock:
                if (
                    process_id in self._sessions
                    or process_id in self._retired_process_ids
                    or self._stop
                ):
                    transport.close()
                    return
                self._sessions[process_id] = session
                current_breakpoints = tuple(self._all_breakpoints())
            for breakpoint in current_breakpoints:
                if breakpoint not in breakpoints:
                    transport.request("setBreakpoints", breakpoint)
            (self._registry_path / f"debugpy-{process_id}.ready").touch()
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
        if name == "continued":
            with self._lock:
                exposed = process_id in self._handoff_exposed
                user_resuming = process_id in self._handoff_user_resuming
                step_pending = process_id in self._handoff_steps
                if exposed and user_resuming and not step_pending:
                    self._handoff_targets.pop(process_id, None)
                    self._handoff_exposed.discard(process_id)
                    self._handoff_user_resuming.discard(process_id)
        if name == "stopped":
            with self._lock:
                step = self._handoff_steps.get(process_id)
                targeted = process_id in self._handoff_targets
                exposed = process_id in self._handoff_exposed
                resolving = process_id in self._handoff_resolving
                if (targeted or step is not None) and not resolving:
                    self._handoff_resolving.add(process_id)
            if step is not None:
                if not resolving:
                    Thread(
                        target=self._resolve_handoff_step,
                        args=(process_id, body),
                        name=f"pyrust-debugpy-step-{process_id}",
                        daemon=True,
                    ).start()
                return
            if targeted:
                if resolving:
                    if not exposed:
                        with self._lock:
                            self._handoff_pending_stops[process_id] = body
                    return
                if exposed:
                    with self._lock:
                        self._handoff_resolving.discard(process_id)
                else:
                    Thread(
                        target=self._emit_targeted_handoff_stop,
                        args=(process_id, body),
                        name=f"pyrust-debugpy-target-stop-{process_id}",
                        daemon=True,
                    ).start()
                return
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
            (self._registry_path / f"handoff-entered-{process_id}").unlink(
                missing_ok=True
            )
            (self._registry_path / f"handoff-ready-{process_id}").unlink(
                missing_ok=True
            )
            self._state.on_stopped({"body": body}, owner="python")
        elif name == "continued":
            if was_python_owner:
                self._state.on_continued({"body": body}, owner="python")
            if body.get("allThreadsContinued") is False and raw_thread_id is not None:
                self._clear_thread_routes(process_id, raw_thread_id)
            else:
                self._clear_process_routes(process_id)
        elif name in {"exited", "terminated"}:
            (self._registry_path / f"handoff-entered-{process_id}").unlink(
                missing_ok=True
            )
            (self._registry_path / f"handoff-ready-{process_id}").unlink(
                missing_ok=True
            )
            (self._registry_path / f"debugpy-{process_id}.ready").unlink(
                missing_ok=True
            )
            self._clear_process_routes(process_id)
            with self._lock:
                exited_session = self._sessions.pop(process_id, None)
                self._retired_process_ids.add(process_id)
                self._recent_frames.pop(process_id, None)
                self._handoff_targets.pop(process_id, None)
                self._handoff_exposed.discard(process_id)
                self._handoff_user_resuming.discard(process_id)
                self._handoff_resolving.discard(process_id)
                self._handoff_pending_stops.pop(process_id, None)
                self._handoff_entry_breakpoints.pop(process_id, None)
                self._handoff_completed_targets = {
                    key
                    for key in self._handoff_completed_targets
                    if key[0] != process_id
                }
                self._handoff_steps.pop(process_id, None)
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

    def _resolve_handoff_step(
        self,
        process_id: int,
        body: dict[str, Any],
    ) -> None:
        """Hide remote-helper stops until debugpy reaches the user target."""

        try:
            session = self._require_session(process_id)
            with self._lock:
                step = self._handoff_steps.get(process_id)
            if step is None:
                return
            response = session.transport.request("threads")
            threads = (response.get("body") or {}).get("threads")
            target_thread_id: int | None = None
            if isinstance(threads, list):
                for thread in threads:
                    candidate = thread.get("id") if isinstance(thread, dict) else None
                    if not isinstance(candidate, int) or candidate <= 0:
                        continue
                    stack = session.transport.request(
                        "stackTrace",
                        {"threadId": candidate, "startFrame": 0, "levels": 200},
                    )
                    frames = (stack.get("body") or {}).get("stackFrames")
                    if not isinstance(frames, list):
                        continue
                    if any(
                        isinstance(frame, dict)
                        and frame.get("name") == step.target_name
                        and frame.get("line") == step.target_line
                        and isinstance(frame.get("source"), dict)
                        and frame["source"].get("path") == step.target_path
                        for frame in frames
                    ):
                        target_thread_id = candidate
                        break
            if target_thread_id is None:
                # The helper's pause/breakpoint is not user-visible. Continue
                # the selected Python thread until its temporary source
                # breakpoint is reached.
                session.resume_ready.clear()
                session.transport.request_async(
                    "continue",
                    {"threadId": step.thread_id, "singleThread": True},
                    on_error=lambda error: self._on_resume_error(
                        session,
                        "handoff step continue",
                        error,
                    ),
                    on_complete=session.resume_ready.set,
                )
                return
            self._restore_handoff_breakpoint(process_id, step)
            virtual_thread_id = self._record_thread(process_id, target_thread_id)
            final_body = dict(body)
            final_body.update(
                {
                    "reason": "step",
                    "description": f"Python {step.command}",
                    "threadId": virtual_thread_id,
                    "systemProcessId": process_id,
                }
            )
            with self._lock:
                self._handoff_steps.pop(process_id, None)
                self._handoff_targets.pop(process_id, None)
                self._handoff_exposed.discard(process_id)
                self._handoff_user_resuming.discard(process_id)
            (self._registry_path / f"handoff-entered-{process_id}").unlink(
                missing_ok=True
            )
            (self._registry_path / f"handoff-ready-{process_id}").unlink(
                missing_ok=True
            )
            self._state.on_stopped({"body": final_body}, owner="python")
            self._emit_event("stopped", final_body)
        except PythonTransportError as error:
            self._emit_event(
                "output",
                {
                    "category": "stderr",
                    "output": f"PyRust could not complete Python handoff step: {error}\n",
                },
            )
        finally:
            with self._lock:
                self._handoff_resolving.discard(process_id)

    def _emit_targeted_handoff_stop(
        self,
        process_id: int,
        body: dict[str, Any],
    ) -> None:
        while True:
            try:
                self._resolve_targeted_handoff_stop(process_id, body)
            except PythonTransportError as error:
                with self._lock:
                    pending = self._handoff_pending_stops.pop(process_id, None)
                    if pending is None:
                        self._handoff_resolving.discard(process_id)
                if pending is not None:
                    body = pending
                    continue
                (
                    self._registry_path / f"handoff-release-{process_id}"
                ).touch()
                self._restore_entry_breakpoint(process_id)
                self._emit_event(
                    "output",
                    {
                        "category": "stderr",
                        "output": (
                            "PyRust could not resolve the selected Python "
                            f"handoff thread for process {process_id}: {error}\n"
                        ),
                    },
                )
                return
            with self._lock:
                # A second pause/breakpoint event for the same handoff is a
                # duplicate once the selected frame has been resolved.
                self._handoff_pending_stops.pop(process_id, None)
                self._handoff_resolving.discard(process_id)
            return

    def _resolve_targeted_handoff_stop(
        self,
        process_id: int,
        body: dict[str, Any],
    ) -> None:
        raw_thread_id = body.get("threadId")
        session = self._require_session(process_id)
        with self._lock:
            target = self._handoff_targets.get(process_id)
        if target is not None:
            target_name, target_path = target
            target_thread_id: int | None = None
            deadline = time.monotonic() + 10
            while target_thread_id is None:
                response = session.transport.request("threads")
                threads = (response.get("body") or {}).get("threads")
                if isinstance(threads, list):
                    for thread in threads:
                        if not isinstance(thread, dict):
                            continue
                        candidate = thread.get("id")
                        if (
                            not isinstance(candidate, int)
                            or isinstance(candidate, bool)
                            or candidate <= 0
                        ):
                            continue
                        stack = session.transport.request(
                            "stackTrace",
                            {
                                "threadId": candidate,
                                "startFrame": 0,
                                "levels": 200,
                            },
                        )
                        frames = (stack.get("body") or {}).get("stackFrames")
                        if not isinstance(frames, list):
                            continue
                        if any(
                            isinstance(frame, dict)
                            and frame.get("name") == target_name
                            and isinstance(frame.get("source"), dict)
                            and frame["source"].get("path") == target_path
                            for frame in frames
                        ):
                            target_thread_id = candidate
                            break
                if target_thread_id is not None:
                    raw_thread_id = target_thread_id
                    break
                if time.monotonic() >= deadline:
                    raise PythonTransportError(
                        "debugpy stopped without the selected Python frame "
                        f"{target_name!r} in {target_path!r}"
                    )
                time.sleep(0.01)
        if (
            not isinstance(raw_thread_id, int)
            or isinstance(raw_thread_id, bool)
            or raw_thread_id <= 0
        ):
            raise PythonTransportError(
                f"debugpy process {process_id} stopped without a thread"
            )
        virtual_thread_id = self._record_thread(
            process_id,
            raw_thread_id,
        )
        body["threadId"] = virtual_thread_id
        body["systemProcessId"] = process_id
        self._restore_entry_breakpoint(process_id)
        (self._registry_path / f"handoff-release-{process_id}").touch()
        self._state.on_stopped({"body": body}, owner="python")
        with self._lock:
            self._handoff_exposed.add(process_id)
            target = self._handoff_targets.get(process_id)
            if target is not None:
                self._handoff_completed_targets.add(
                    (process_id, target[0], target[1])
                )
            # Publish the stop only after the previous resolver generation is
            # fully closed, so a fast client cannot start the next handoff
            # while this one still appears active internally.
            self._handoff_pending_stops.pop(process_id, None)
            self._handoff_resolving.discard(process_id)
        self._emit_event("stopped", body)

    def _restore_entry_breakpoint(self, process_id: int) -> None:
        with self._lock:
            entry = self._handoff_entry_breakpoints.pop(process_id, None)
        if entry is None:
            return
        self._restore_handoff_breakpoint(process_id, entry)

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


def _next_python_statement_line(path: str, current_line: int) -> int | None:
    """Find the following statement in the innermost source block."""

    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    candidates: list[ast.AST] = [tree]
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for statement in body:
            start = getattr(statement, "lineno", None)
            end = getattr(statement, "end_lineno", start)
            if isinstance(start, int) and isinstance(end, int) and start <= current_line <= end:
                candidates.append(node)
                break
    for container in reversed(candidates):
        body = getattr(container, "body", None)
        if not isinstance(body, list):
            continue
        for index, statement in enumerate(body):
            start = getattr(statement, "lineno", None)
            end = getattr(statement, "end_lineno", start)
            if (
                isinstance(start, int)
                and isinstance(end, int)
                and start <= current_line <= end
                and index + 1 < len(body)
            ):
                next_line = getattr(body[index + 1], "lineno", None)
                if isinstance(next_line, int) and next_line > 0:
                    return next_line
    return None


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
            if not _process_exists(payload["pid"]):
                candidate.unlink(missing_ok=True)
                candidate.with_suffix(".failed").unlink(missing_ok=True)
                candidate.with_suffix(".ready").unlink(missing_ok=True)
                continue
            records.append(payload)
    return tuple(records)


def _process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _default_transport_factory(
    event_handler: Callable[[Message], None],
) -> DebugpyTransport:
    return DebugpyTransport(event_handler=event_handler)
