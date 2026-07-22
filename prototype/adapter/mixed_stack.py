"""Fixture-bound Python/Rust stack augmentation for the first workable slice."""

from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import dataclass
import importlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from typing import Any, Mapping

from .child_transport import ChildTransportError
from .python_manager import PythonProcessManager
from .python_transport import PythonTransportError
from .process_manager import ProcessManager
from prototype.python.pyrust_stack import (
    LocalReadError,
    StackReadError,
    read_python_locals,
    read_python_stacks,
)

from .proxy import LocalResponse, Message, ProxyContext, ProxyHooks


class HelperFailure(RuntimeError):
    """A bounded Python-stack collection failure."""


class HelperTimeout(HelperFailure):
    """The configured external helper exceeded its deadline."""


class InProcessHelperTimeout(HelperTimeout):
    """The in-process unwinder timed out and opened its session circuit."""


class InProcessUnwinderUnavailable(HelperFailure):
    """The in-process unwinder cannot be used for this stack request."""


class StaleStopError(RuntimeError):
    """The debuggee continued while a stack request was in flight."""


class PythonExpressionError(ValueError):
    """A request cannot be evaluated from a frozen Python local snapshot."""


@dataclass
class _NativeLeaseFrame:
    process_id: int
    native_thread_id: int
    python_thread_id: int
    name: str
    path: str
    line: int
    instruction_pointer_reference: str
    frame_id: int | None = None


@dataclass(frozen=True)
class _NativeLeaseStep:
    process_id: int
    thread_id: int
    command: str
    arguments: dict[str, Any]


class MixedStackHooks(ProxyHooks):
    """Merge one CPython stack into either supported fixture stack."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._helper_command: str | None = None
        self._helper_timeout_ms = 1_000
        self._stopped_thread_id: int | None = None
        # VS Code may omit frameId for Debug Console input. A scopes request is
        # its reliable signal that a user selected a Call Stack frame.
        self._console_frame_id: int | None = None
        self._diagnosed_epochs: set[int] = set()
        self._in_process_worker_active = False
        self._in_process_circuit_open = False
        self._in_process_timeout_diagnosed = False
        self._process_manager: ProcessManager | None = None
        self._python_manager: PythonProcessManager | None = None
        self._python_debug_registry: TemporaryDirectory[str] | None = None
        self._child_only_process: subprocess.Popen[bytes] | None = None
        self._child_only_mode = False
        self._restart_arguments: dict[str, Any] | None = None
        self._function_breakpoints: list[dict[str, Any]] = []
        self._temporary_native_step_target: str | None = None
        self._instruction_breakpoints: list[dict[str, Any]] = []
        self._pending_native_step: _NativeLeaseStep | None = None
        self._python_handoffs: set[tuple[int, int]] = set()
        self._python_step_in_processes: set[int] = set()
        self._python_step_in_suppress_python: set[int] = set()
        self._native_maintenance: dict[int, Event] = {}
        self._native_suppress_continued: dict[int, Event] = {}
        self._native_active: dict[int, tuple[int, int]] = {}
        self._native_lease_frames: dict[int, _NativeLeaseFrame] = {}
        self._native_lease_issued: set[int] = set()
        self._native_lease_next = 1_900_000_000
        self._source_root = Path.cwd()

    def on_launch(
        self,
        request: Message,
        context: ProxyContext,
    ) -> Message | LocalResponse:
        outgoing = dict(request)
        arguments = dict(outgoing.get("arguments") or {})
        helper_command = arguments.pop("pyrustHelperCommand", None)
        helper_timeout_ms = arguments.pop("pyrustHelperTimeoutMs", None)
        child_registry = arguments.pop("pyrustChildRegistryPath", None)
        process_mode = arguments.pop("pyrustProcessMode", "native")
        thread_mode = arguments.pop("pyrustThreadMode", "all")
        python_debug = arguments.pop("pyrustPythonDebug", True)
        python_debug_registry = arguments.pop("pyrustPythonDebugRegistry", None)

        if helper_command is not None and not isinstance(helper_command, str):
            raise ValueError("pyrustHelperCommand must be a string")
        if helper_timeout_ms is not None and (
            not isinstance(helper_timeout_ms, int)
            or isinstance(helper_timeout_ms, bool)
            or helper_timeout_ms <= 0
        ):
            raise ValueError("pyrustHelperTimeoutMs must be a positive integer")
        if child_registry is not None and (
            not isinstance(child_registry, str) or not child_registry
        ):
            raise ValueError("pyrustChildRegistryPath must be a non-empty string")
        if process_mode not in {"native", "children"}:
            raise ValueError("pyrustProcessMode must be 'native' or 'children'")
        if process_mode == "children" and child_registry is None:
            raise ValueError(
                "pyrustProcessMode 'children' requires pyrustChildRegistryPath"
            )
        if thread_mode not in {"all", "single"}:
            raise ValueError("pyrustThreadMode must be 'all' or 'single'")
        if not isinstance(python_debug, bool):
            raise ValueError("pyrustPythonDebug must be a boolean")
        if python_debug_registry is not None and (
            not isinstance(python_debug_registry, str) or not python_debug_registry
        ):
            raise ValueError(
                "pyrustPythonDebugRegistry must be a non-empty string"
            )
        if process_mode == "children":
            _prepare_child_registry(Path(child_registry))
        program = arguments.get("program")
        process_metadata = (
            _launch_process_metadata(program, arguments.get("args"))
            if isinstance(program, str)
            else None
        )

        with self._lock:
            self._helper_command = helper_command
            self._helper_timeout_ms = helper_timeout_ms or 1_000
            self._stopped_thread_id = None
            self._console_frame_id = None
            self._diagnosed_epochs.clear()
            self._child_only_mode = process_mode == "children"
            self._restart_arguments = None
            self._temporary_native_step_target = None
            self._instruction_breakpoints.clear()
            self._pending_native_step = None
            self._python_handoffs.clear()
            self._python_step_in_processes.clear()
            self._python_step_in_suppress_python.clear()
            self._native_maintenance.clear()
            self._native_suppress_continued.clear()
            self._native_active.clear()
            self._native_lease_frames.clear()
            self._native_lease_issued.clear()
            self._source_root = Path(
                arguments.get("cwd")
                if isinstance(arguments.get("cwd"), str)
                else Path.cwd()
            ).resolve()
            if process_metadata is not None:
                context.state.set_default_process_metadata(*process_metadata)
            if self._process_manager is not None:
                self._process_manager.close()
                self._process_manager = None
            if self._python_manager is not None:
                self._python_manager.close()
                self._python_manager = None
            if self._python_debug_registry is not None:
                self._python_debug_registry.cleanup()
                self._python_debug_registry = None
            self._terminate_child_only_process()
            if python_debug:
                registry_path = self._configure_python_debug(
                    arguments,
                    child_registry=child_registry,
                    configured_registry=python_debug_registry,
                )
                self._python_manager = PythonProcessManager(
                    registry_path=registry_path,
                    state=context.state,
                    emit_event=lambda event, body: self._emit_python_event(
                        context,
                        event,
                        body,
                    ),
                )
            if self._child_only_mode:
                self._child_only_process = self._start_child_only_parent(
                    arguments,
                    context,
                )
                parent_role = (
                    f"{process_metadata[1].removesuffix(' process')} parent process"
                    if process_metadata is not None
                    else "Rust parent process"
                )
                context.state.register_process(
                    self._child_only_process.pid,
                    display_name=parent_role,
                    role=parent_role,
                    command=(
                        process_metadata[2]
                        if process_metadata is not None
                        else None
                    ),
                )
            if child_registry is not None:
                self._process_manager = ProcessManager(
                    registry_path=Path(child_registry),
                    adapter_command=_child_codelldb_command(),
                    cwd=str(Path(__file__).resolve().parents[2]),
                    state=context.state,
                    force_single_thread=thread_mode == "single",
                    emit_event=lambda event, body: self._emit_child_event(
                        context,
                        event,
                        body,
                    ),
                )
            if self._child_only_mode:
                context.send_event("initialized")
                return LocalResponse(body={})
            self._restart_arguments = deepcopy(arguments)
        outgoing["arguments"] = arguments
        return outgoing

    def on_stopped(
        self,
        event: Message,
        context: ProxyContext,
    ) -> Message | None:
        self._restore_function_breakpoints(context)
        thread_id = (event.get("body") or {}).get("threadId")
        process_id = (event.get("body") or {}).get("systemProcessId")
        if not isinstance(process_id, int) and isinstance(thread_id, int):
            process_id = context.state.process_id_for_thread(thread_id)
        native_step = self._restore_instruction_breakpoints(
            process_id if isinstance(process_id, int) else None,
            context,
        )
        if native_step is not None:
            try:
                self._continue_native_lease_step(native_step, context)
            except (ChildTransportError, TimeoutError) as error:
                event = deepcopy(event)
                body = dict(event.get("body") or {})
                body["reason"] = "pause"
                body["description"] = f"Rust step failed: {error}"
                event["body"] = body
            else:
                return None
        if isinstance(process_id, int):
            with self._lock:
                maintenance = self._native_maintenance.get(process_id)
            if maintenance is not None:
                maintenance.set()
                return None
        python_step_in = False
        if isinstance(process_id, int):
            with self._lock:
                python_step_in = process_id in self._python_step_in_processes
                if python_step_in:
                    self._python_step_in_processes.discard(process_id)
                    self._python_step_in_suppress_python.discard(process_id)
                    self._python_handoffs = {
                        key for key in self._python_handoffs if key[0] != process_id
                    }
            if python_step_in:
                event = deepcopy(event)
                body = dict(event.get("body") or {})
                body["reason"] = "step"
                body["description"] = "Stepped from Python into Rust"
                event["body"] = body
        if (
            isinstance(process_id, int)
            and isinstance(thread_id, int)
            and not python_step_in
            and self._is_python_handoff_process(process_id)
        ):
            manager = self._process_manager
            if manager is not None and manager.owns_thread(thread_id):
                Thread(
                    target=self._continue_child_handoff,
                    args=(manager, thread_id, process_id),
                    name=f"pyrust-python-handoff-{process_id}",
                    daemon=True,
                ).start()
            else:
                try:
                    response = context.request_downstream(
                        "continue",
                        {"threadId": thread_id, "singleThread": True},
                        timeout=10,
                    )
                    if response.get("success") is True:
                        context.state.on_continued(
                            {
                                "body": {
                                    "threadId": thread_id,
                                    "systemProcessId": process_id,
                                }
                            }
                        )
                except Exception:
                    pass
            return None
        with self._lock:
            self._stopped_thread_id = (
                thread_id
                if isinstance(thread_id, int)
                and not isinstance(thread_id, bool)
                and thread_id > 0
                else None
            )
            self._console_frame_id = None
        return event

    def on_continued(
        self,
        event: Message,
        context: ProxyContext,
    ) -> Message | None:
        thread_id = (event.get("body") or {}).get("threadId")
        process_id = (event.get("body") or {}).get("systemProcessId")
        if not isinstance(process_id, int) and isinstance(thread_id, int):
            process_id = context.state.process_id_for_thread(thread_id)
        if isinstance(process_id, int):
            with self._lock:
                continued = self._native_suppress_continued.pop(process_id, None)
                if continued is not None:
                    continued.set()
                    return None
                self._native_active.pop(process_id, None)
                self._native_lease_frames = {
                    frame_id: route
                    for frame_id, route in self._native_lease_frames.items()
                    if route.process_id != process_id
                }
        with self._lock:
            self._stopped_thread_id = None
            self._console_frame_id = None
        return event

    def on_threads(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        del request, context
        python_manager = self._python_manager
        if python_manager is not None and python_manager.has_python_stop():
            return LocalResponse(body={"threads": python_manager.threads()})
        if self._process_manager is None:
            return None
        return LocalResponse(body={"threads": self._process_manager.threads()})

    def on_continue_request(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        thread_id = (request.get("arguments") or {}).get("threadId")
        python_manager = self._python_manager
        if python_manager is not None and python_manager.owns_thread(thread_id):
            assert isinstance(thread_id, int)
            process_id = context.state.process_id_for_thread(thread_id)
            if isinstance(process_id, int):
                self._release_native_lease(process_id, context)
            try:
                response = python_manager.continue_thread(
                    thread_id,
                    single_thread=bool(
                        (request.get("arguments") or {}).get("singleThread")
                    ),
                )
            except PythonTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        manager = self._process_manager
        if manager is not None and manager.owns_thread(thread_id):
            assert isinstance(thread_id, int)
            requested_single_thread = (request.get("arguments") or {}).get(
                "singleThread"
            )
            try:
                response = manager.continue_thread(
                    thread_id,
                    single_thread=bool(requested_single_thread),
                )
            except Exception as error:
                return LocalResponse(
                    success=False,
                    message=f"child continue failed: {error!r}",
                )
            return LocalResponse(body=dict(response.get("body") or {}))

        # CodeLLDB may emit continued after a fast async future has already
        # reached its next stop. Once native continue succeeds, invalidate the
        # old synthetic Python frames synchronously for this DAP client.
        try:
            response = context.request_downstream(
                "continue",
                dict(request.get("arguments") or {}),
                timeout=10,
            )
        except Exception as error:
            return LocalResponse(success=False, message=f"native continue failed: {error}")
        if response.get("success") is not True:
            return LocalResponse(
                success=False,
                body=response.get("body"),
                message=str(response.get("message", "native continue failed")),
            )
        context.state.on_continued(
            {"body": {"threadId": thread_id}}
            if isinstance(thread_id, int)
            else None
        )
        return LocalResponse(body=dict(response.get("body") or {}))

    def on_step_request(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        thread_id = (request.get("arguments") or {}).get("threadId")
        with self._lock:
            selected_frame_id = self._console_frame_id
            selected_native = self._native_lease_frames.get(selected_frame_id)
        if (
            selected_native is not None
            and thread_id == selected_native.python_thread_id
        ):
            return self._step_from_native_lease(
                selected_native,
                request,
                context,
            )
        python_manager = self._python_manager
        if python_manager is not None and python_manager.owns_thread(thread_id):
            assert isinstance(thread_id, int)
            if python_manager.is_handoff_exposed(thread_id):
                command = str(request.get("command"))
                if command != "stepIn":
                    try:
                        response = python_manager.begin_handoff_step(
                            thread_id,
                            command,
                        )
                    except PythonTransportError as error:
                        return LocalResponse(success=False, message=str(error))
                    return LocalResponse(body=dict(response.get("body") or {}))
                process_id: int | None = None
                try:
                    process_id, _native_thread_id = (
                        python_manager.native_identity_for_thread(thread_id)
                    )
                    with self._lock:
                        self._python_step_in_processes.add(process_id)
                        self._python_step_in_suppress_python.add(process_id)
                    response = python_manager.continue_thread(
                        thread_id,
                        single_thread=True,
                    )
                except PythonTransportError as error:
                    if process_id is not None:
                        with self._lock:
                            self._python_step_in_processes.discard(process_id)
                            self._python_step_in_suppress_python.discard(process_id)
                    return LocalResponse(success=False, message=str(error))
                return LocalResponse(body=dict(response.get("body") or {}))
            process_id = context.state.process_id_for_thread(thread_id)
            if isinstance(process_id, int):
                self._release_native_lease(process_id, context)
            if request.get("command") == "stepIn":
                self._prepare_native_step_target(
                    python_manager,
                    thread_id,
                    context,
                )
            try:
                response = python_manager.step_thread(
                    str(request["command"]),
                    thread_id,
                    request.get("arguments") or {},
                )
            except PythonTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        manager = self._process_manager
        if manager is not None and manager.owns_thread(thread_id):
            assert isinstance(thread_id, int)
            try:
                response = manager.step_thread(
                    str(request["command"]),
                    thread_id,
                    request.get("arguments") or {},
                )
            except ChildTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        return None

    def on_set_function_breakpoints(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        del context
        breakpoints = (request.get("arguments") or {}).get("breakpoints")
        with self._lock:
            self._function_breakpoints = (
                [dict(item) for item in breakpoints if isinstance(item, dict)]
                if isinstance(breakpoints, list)
                else []
            )
            self._temporary_native_step_target = None
        return None

    def on_set_instruction_breakpoints(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        del context
        breakpoints = (request.get("arguments") or {}).get("breakpoints")
        with self._lock:
            self._instruction_breakpoints = (
                [dict(item) for item in breakpoints if isinstance(item, dict)]
                if isinstance(breakpoints, list)
                else []
            )
            self._pending_native_step = None
        return None

    def on_pause_request(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        thread_id = (request.get("arguments") or {}).get("threadId")
        python_manager = self._python_manager
        if python_manager is not None and python_manager.owns_thread(thread_id):
            assert isinstance(thread_id, int)
            try:
                response = python_manager.pause_thread(thread_id)
            except PythonTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        manager = self._process_manager
        if manager is not None and manager.owns_thread(thread_id):
            assert isinstance(thread_id, int)
            try:
                response = manager.pause_thread(thread_id)
            except ChildTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        return None

    def on_restart(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse:
        del request
        with self._lock:
            if self._child_only_mode:
                return LocalResponse(
                    success=False,
                    message="restart is not supported for child-only process mode",
                )
            restart_arguments = (
                deepcopy(self._restart_arguments)
                if self._restart_arguments is not None
                else None
            )
            self._stopped_thread_id = None
            self._console_frame_id = None
        if restart_arguments is None:
            return LocalResponse(
                success=False,
                message="restart is unavailable before a successful launch",
            )
        if self._python_manager is not None:
            self._python_manager.prepare_restart()
        try:
            response = context.request_downstream(
                "restart",
                {"arguments": restart_arguments},
                timeout=30,
            )
        except Exception as error:
            return LocalResponse(
                success=False,
                message=f"native restart failed: {error}",
            )
        if response.get("success") is not True:
            return LocalResponse(
                success=False,
                body=response.get("body"),
                message=str(response.get("message", "native restart failed")),
            )
        return LocalResponse(body=dict(response.get("body") or {}))

    def on_set_breakpoints(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        source = (request.get("arguments") or {}).get("source")
        source_path = source.get("path") if isinstance(source, dict) else None
        python_manager = self._python_manager
        if (
            python_manager is not None
            and isinstance(source_path, str)
            and source_path.lower().endswith((".py", ".pyw"))
        ):
            python_manager.add_breakpoints(request.get("arguments") or {})
            breakpoints = (request.get("arguments") or {}).get("breakpoints")
            count = len(breakpoints) if isinstance(breakpoints, list) else 0
            return LocalResponse(
                body={
                    "breakpoints": [
                        {
                            "verified": True,
                            "message": "Queued for registered debugpy processes",
                        }
                        for _ in range(count)
                    ]
                }
            )
        del context
        if self._process_manager is not None:
            self._process_manager.add_breakpoints(request.get("arguments") or {})
        if self._child_only_mode:
            breakpoints = (request.get("arguments") or {}).get("breakpoints")
            count = len(breakpoints) if isinstance(breakpoints, list) else 0
            return LocalResponse(
                body={
                    "breakpoints": [
                        {
                            "verified": False,
                            "message": (
                                "Breakpoint will be resolved in registered "
                                "child processes"
                            ),
                        }
                        for _ in range(count)
                    ]
                }
            )
        return None

    def on_set_variable(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        reference = (request.get("arguments") or {}).get("variablesReference")
        python_manager = self._python_manager
        if (
            python_manager is not None
            and python_manager.variable_route(reference) is not None
        ):
            route = python_manager.variable_route(reference)
            assert route is not None
            self._release_native_lease(route.process_id, context)
            assert isinstance(reference, int)
            try:
                response = python_manager.set_variable(
                    reference,
                    request.get("arguments") or {},
                )
            except PythonTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        child_manager = self._process_manager
        if (
            child_manager is not None
            and child_manager.variable_route(reference) is not None
        ):
            assert isinstance(reference, int)
            try:
                response = child_manager.set_variable(
                    reference,
                    request.get("arguments") or {},
                )
            except ChildTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        if context.synthetic_frames.get(reference) is not None:
            handoff = self._handoff_snapshot_to_debugpy(reference, context)
            if handoff is not None and handoff.success:
                return LocalResponse(
                    success=False,
                    message=(
                        "Python frame transferred to debugpy; retry assignment "
                        "on the refreshed live frame"
                    ),
                )
            return handoff
        return None

    def on_set_expression(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        frame_id = (request.get("arguments") or {}).get("frameId")
        native_lease = (
            self._native_lease_frames.get(frame_id)
            if isinstance(frame_id, int)
            else None
        )
        if native_lease is not None:
            try:
                native_id = self._acquire_native_lease(native_lease, context)
                arguments = dict(request.get("arguments") or {})
                arguments["frameId"] = native_id
                child_manager = self._process_manager
                response = (
                    child_manager.set_expression(native_id, arguments)
                    if child_manager is not None
                    and child_manager.native_frame_route(native_id) is not None
                    else context.request_downstream(
                        "setExpression",
                        arguments,
                        timeout=10,
                    )
                )
            except (ChildTransportError, PythonTransportError, TimeoutError) as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        if isinstance(frame_id, int) and frame_id in self._native_lease_issued:
            return LocalResponse(
                success=False,
                message="Rust frame is no longer valid for this debug stop",
            )
        python_manager = self._python_manager
        if (
            python_manager is not None
            and python_manager.native_frame_route(frame_id) is not None
        ):
            assert isinstance(frame_id, int)
            route = python_manager.native_frame_route(frame_id)
            assert route is not None
            self._release_native_lease(route.process_id, context)
            try:
                response = python_manager.set_expression(
                    frame_id,
                    request.get("arguments") or {},
                )
            except PythonTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        if self._current_python_frame_id(frame_id, context) is not None:
            handoff = self._handoff_snapshot_to_debugpy(frame_id, context)
            if handoff is not None and handoff.success:
                return LocalResponse(
                    success=False,
                    message=(
                        "Python frame transferred to debugpy; retry assignment "
                        "on the refreshed live frame"
                    ),
                )
            return handoff
        return None

    def on_configuration_done(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        del request, context
        if self._python_manager is not None:
            self._python_manager.mark_configuration_done()
        if self._process_manager is not None:
            self._process_manager.mark_configuration_done()
        if self._child_only_mode:
            return LocalResponse(body={})
        return None

    def on_process_tree(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse:
        del request
        python_manager = self._python_manager
        if python_manager is not None and python_manager.has_python_stop():
            python_manager.refresh_threads()
        manager = self._process_manager
        if manager is not None:
            manager.refresh_threads()
        else:
            # The custom view is refreshed independently of VS Code's built-in
            # Call Stack. Refresh native threads here so both views start from
            # the same CodeLLDB thread inventory.
            try:
                response = context.request_downstream("threads", {}, timeout=5)
                if response.get("success") is True:
                    context.state.record_threads_response(response)
            except Exception:
                # Retain the stopped-thread snapshot if CodeLLDB is between
                # lifecycle states or cannot answer this optional refresh.
                pass
        return LocalResponse(body={"processes": context.state.process_tree()})

    def on_scopes(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        frame_id = (request.get("arguments") or {}).get("frameId")
        if isinstance(frame_id, int) and not isinstance(frame_id, bool):
            with self._lock:
                self._console_frame_id = frame_id
        native_lease = (
            self._native_lease_frames.get(frame_id)
            if isinstance(frame_id, int)
            else None
        )
        if native_lease is not None:
            try:
                native_id = self._acquire_native_lease(native_lease, context)
                manager = self._process_manager
                response = (
                    manager.scopes(native_id)
                    if manager is not None
                    and manager.native_frame_route(native_id) is not None
                    else context.request_downstream(
                        "scopes",
                        {"frameId": native_id},
                        timeout=10,
                    )
                )
            except (ChildTransportError, PythonTransportError, TimeoutError) as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        if isinstance(frame_id, int) and frame_id in self._native_lease_issued:
            return LocalResponse(
                success=False,
                message="Rust frame is no longer valid for this debug stop",
            )
        python_manager = self._python_manager
        if (
            python_manager is not None
            and python_manager.native_frame_route(frame_id) is not None
        ):
            assert isinstance(frame_id, int)
            route = python_manager.native_frame_route(frame_id)
            assert route is not None
            self._release_native_lease(route.process_id, context)
            try:
                response = python_manager.scopes(frame_id)
            except PythonTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        manager = self._process_manager
        if manager is not None and manager.native_frame_route(frame_id) is not None:
            assert isinstance(frame_id, int)
            try:
                response = manager.scopes(frame_id)
            except ChildTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        frame = self._current_python_frame_id(frame_id, context)
        if frame is None:
            return None
        handoff = self._handoff_snapshot_to_debugpy(frame_id, context)
        if handoff is not None:
            return handoff
        locals_snapshot = frame.get("locals")
        if not isinstance(locals_snapshot, dict):
            return LocalResponse(
                body={
                    "scopes": [
                        {
                            "name": "Python Locals (unavailable)",
                            "presentationHint": "locals",
                            "variablesReference": 0,
                            "expensive": False,
                        }
                    ]
                }
            )
        return LocalResponse(
            body={
                "scopes": [
                    {
                        "name": "Python Locals",
                        "presentationHint": "locals",
                        # The high synthetic frame ID is proxy-owned and is
                        # reused only as a scope reference for this stop.
                        "variablesReference": request["arguments"]["frameId"],
                        "expensive": False,
                    }
                ]
            }
        )

    def on_variables(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        reference = (request.get("arguments") or {}).get("variablesReference")
        if not isinstance(reference, int):
            return None
        python_manager = self._python_manager
        if (
            python_manager is not None
            and python_manager.variable_route(reference) is not None
        ):
            route = python_manager.variable_route(reference)
            assert route is not None
            self._release_native_lease(route.process_id, context)
            try:
                response = python_manager.variables(reference)
            except PythonTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        manager = self._process_manager
        if manager is not None and manager.variable_route(reference) is not None:
            try:
                response = manager.variables(reference)
            except ChildTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        synthetic = context.synthetic_frames.get(reference)
        if synthetic is None:
            return None
        handoff = self._handoff_snapshot_to_debugpy(reference, context)
        if handoff is not None:
            return handoff
        frame = synthetic.value
        if not isinstance(frame, dict):
            return LocalResponse(
                success=False,
                message="synthetic Python frame has invalid local state",
            )
        locals_snapshot = frame.get("locals")
        if not isinstance(locals_snapshot, dict):
            return LocalResponse(
                success=False,
                message=str(
                    frame.get(
                        "localsError",
                        "Python locals are unavailable for this frame",
                    )
                ),
            )
        variables = [
            _dap_variable(name, value)
            for name, value in sorted(locals_snapshot.items())
            if isinstance(name, str)
        ]
        return LocalResponse(body={"variables": variables})

    def on_evaluate(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        frame_id = self._console_frame_id_for(request)
        native_lease = (
            self._native_lease_frames.get(frame_id)
            if isinstance(frame_id, int)
            else None
        )
        if native_lease is not None:
            try:
                native_id = self._acquire_native_lease(native_lease, context)
                arguments = dict(request.get("arguments") or {})
                arguments["frameId"] = native_id
                manager = self._process_manager
                response = (
                    manager.evaluate(native_id, arguments)
                    if manager is not None
                    and manager.native_frame_route(native_id) is not None
                    else context.request_downstream(
                        "evaluate",
                        arguments,
                        timeout=10,
                    )
                )
            except (ChildTransportError, PythonTransportError, TimeoutError) as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        python_manager = self._python_manager
        if (
            python_manager is not None
            and python_manager.native_frame_route(frame_id) is not None
        ):
            assert isinstance(frame_id, int)
            route = python_manager.native_frame_route(frame_id)
            assert route is not None
            self._release_native_lease(route.process_id, context)
            try:
                response = python_manager.evaluate(
                    frame_id,
                    request.get("arguments") or {},
                )
            except PythonTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        manager = self._process_manager
        if manager is not None and manager.native_frame_route(frame_id) is not None:
            assert isinstance(frame_id, int)
            try:
                response = manager.evaluate(frame_id, request.get("arguments") or {})
            except ChildTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        frame = self._current_python_frame_id(frame_id, context)
        if frame is None:
            return None
        handoff = self._handoff_snapshot_to_debugpy(frame_id, context)
        if handoff is not None:
            return handoff
        locals_snapshot = frame.get("locals")
        if not isinstance(locals_snapshot, dict):
            return LocalResponse(
                success=False,
                message=str(
                    frame.get(
                        "localsError",
                        "Python evaluation is unavailable for this frame",
                    )
                ),
            )
        expression = (request.get("arguments") or {}).get("expression")
        if not isinstance(expression, str):
            return LocalResponse(
                success=False,
                message="Python evaluation requires a string expression",
            )
        try:
            value = _evaluate_python_expression(expression, locals_snapshot)
        except PythonExpressionError as error:
            return LocalResponse(success=False, message=str(error))
        return LocalResponse(
            body={
                "result": _display_python_value(value),
                "type": _python_type_name(value),
                "variablesReference": 0,
            }
        )

    def on_stack_trace(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse:
        arguments = request.get("arguments") or {}
        thread_id = arguments.get("threadId")
        if (
            not isinstance(thread_id, int)
            or isinstance(thread_id, bool)
            or thread_id <= 0
        ):
            return LocalResponse(
                success=False,
                message="stackTrace requires a positive threadId",
            )

        manager = self._process_manager
        python_manager = self._python_manager
        if python_manager is not None and python_manager.owns_thread(thread_id):
            try:
                response = python_manager.stack_trace(thread_id, arguments)
            except PythonTransportError as error:
                return LocalResponse(success=False, message=str(error))
            body = dict(response.get("body") or {})
            frames = body.get("stackFrames")
            if isinstance(frames, list):
                try:
                    native_frames = self._capture_native_lease_frames(
                        python_manager,
                        thread_id,
                        context,
                    )
                except (
                    ChildTransportError,
                    PythonTransportError,
                    TimeoutError,
                ) as error:
                    context.send_output(
                        f"PyRust could not capture live Rust frames: {error}\n",
                        category="stderr",
                    )
                    native_frames = []
                if native_frames:
                    merged = [*frames, *native_frames]
                    body["stackFrames"] = merged
                    body["totalFrames"] = len(merged)
            return LocalResponse(body=body)
        child_process = (
            context.state.process_id_for_thread(thread_id)
            if manager is not None and manager.owns_thread(thread_id)
            else None
        )
        synthetic_process = context.state.process_id_for_thread(thread_id)
        request_epoch = context.state.stop_epoch
        synthetic_epoch = None if child_process is not None else request_epoch
        try:
            self._require_epoch(
                context,
                request_epoch,
                process_id=child_process,
                thread_id=thread_id,
            )
        except StaleStopError as error:
            return LocalResponse(success=False, message=str(error))
        try:
            native_response = (
                manager.stack_trace(thread_id, arguments)
                if manager is not None and manager.owns_thread(thread_id)
                else context.request_downstream(
                    "stackTrace",
                    self._native_stack_arguments(arguments, thread_id),
                    timeout=10,
                )
            )
        except ChildTransportError as error:
            return LocalResponse(success=False, message=str(error))
        if native_response.get("success") is not True:
            return LocalResponse(
                success=False,
                body=native_response.get("body"),
                message=str(native_response.get("message", "native stackTrace failed")),
            )

        body = native_response.get("body") or {}
        native_frames = body.get("stackFrames")
        if not isinstance(native_frames, list):
            return LocalResponse(
                success=False,
                message="native stackTrace response has no stackFrames",
            )
        native_frames = [dict(frame) for frame in native_frames]
        native_ids = [
            frame_id
            for frame in native_frames
            if isinstance((frame_id := frame.get("id")), int)
            and not isinstance(frame_id, bool)
        ]
        context.synthetic_frames.reserve_native_ids(native_ids)

        if not self._is_mixed_stack_candidate(native_frames):
            merged = native_frames
        else:
            python_process_id: int | None = None
            try:
                self._require_epoch(
                    context,
                    request_epoch,
                    process_id=child_process,
                    thread_id=thread_id,
                )
                python_process_id = (
                    context.state.process_id_for_thread(thread_id)
                    or self._fallback_process_id(thread_id)
                )
                python_frames = self._read_python_frames(
                    process_id=python_process_id,
                    thread_id=thread_id,
                )
                self._require_epoch(
                    context,
                    request_epoch,
                    process_id=child_process,
                    thread_id=thread_id,
                )
                merged = self._merge_frames(
                    native_frames,
                    python_frames,
                    thread_id,
                    synthetic_process,
                    native_ids,
                    context,
                    synthetic_epoch,
                )
                self._require_epoch(
                    context,
                    request_epoch,
                    process_id=child_process,
                    thread_id=thread_id,
                )
            except StaleStopError as error:
                return LocalResponse(success=False, message=str(error))
            except (
                InProcessUnwinderUnavailable,
                HelperTimeout,
                HelperFailure,
                StackReadError,
            ) as error:
                cached_frames = (
                    python_manager.recent_frames(python_process_id)
                    if python_manager is not None
                    and python_process_id is not None
                    else []
                )
                if cached_frames and self._can_restore_debugpy_snapshot(
                    native_frames,
                    cached_frames,
                ):
                    merged = self._merge_frames(
                        native_frames,
                        cached_frames,
                        thread_id,
                        synthetic_process,
                        native_ids,
                        context,
                        synthetic_epoch,
                    )
                    self._diagnose_once(
                        context,
                        "PyRust used the last debugpy Python stack snapshot "
                        "after CPython remote unwinding failed\n",
                    )
                    return self._page_stack_frames(merged, arguments)
                if isinstance(error, InProcessHelperTimeout):
                    self._diagnose_in_process_timeout_once(context)
                try:
                    self._require_epoch(
                        context,
                        request_epoch,
                        process_id=child_process,
                        thread_id=thread_id,
                    )
                except StaleStopError as stale_error:
                    return LocalResponse(success=False, message=str(stale_error))
                if isinstance(error, InProcessUnwinderUnavailable):
                    pass
                elif isinstance(error, InProcessHelperTimeout):
                    pass
                elif isinstance(error, HelperTimeout):
                    self._diagnose_once(context, f"PyRust helper timeout: {error}")
                else:
                    self._diagnose_once(context, f"PyRust helper failure: {error}")
                merged = native_frames

        return self._page_stack_frames(merged, arguments)

    @staticmethod
    def _page_stack_frames(
        frames: list[dict[str, Any]],
        arguments: Mapping[str, Any],
    ) -> LocalResponse:
        start = arguments.get("startFrame", 0)
        levels = arguments.get("levels")
        start = start if isinstance(start, int) and start >= 0 else 0
        if isinstance(levels, int) and levels > 0:
            page = frames[start : start + levels]
        else:
            page = frames[start:]
        return LocalResponse(body={"stackFrames": page, "totalFrames": len(frames)})

    def _fallback_process_id(self, thread_id: int) -> int:
        # Linux exposes each task's thread-group leader in /proc/<tid>/status.
        # This keeps worker-thread reads directed at the process leader even
        # when CodeLLDB does not emit a DAP process event.
        try:
            for line in Path(f"/proc/{thread_id}/status").read_text(
                encoding="utf-8"
            ).splitlines():
                if line.startswith("Tgid:"):
                    leader = int(line.partition(":")[2].strip())
                    if leader > 0:
                        return leader
        except (OSError, ValueError):
            pass
        # Preserve the fixture fallback for adapters that use OS TIDs as
        # process IDs. Multithread acceptance must prove this is not used for
        # worker threads.
        return thread_id

    def _read_python_frames(
        self,
        *,
        process_id: int,
        thread_id: int,
    ) -> list[dict[str, Any]]:
        with self._lock:
            helper_command = self._helper_command
            timeout_ms = self._helper_timeout_ms

        if helper_command:
            payload = self._run_external_helper(
                helper_command,
                process_id,
                timeout_ms,
            )
            threads = payload.get("threads")
        else:
            threads = self._run_in_process_helper(process_id, timeout_ms)

        if not isinstance(threads, list):
            raise HelperFailure("helper response has no thread list")
        for stack in threads:
            if not isinstance(stack, dict) or stack.get("threadId") != thread_id:
                continue
            frames = stack.get("frames")
            if not isinstance(frames, list):
                raise HelperFailure("helper thread has no frame list")
            validated = [self._validate_python_frame(frame) for frame in frames]
            if helper_command:
                return validated
            return self._attach_local_snapshots(
                validated,
                process_id=process_id,
                thread_id=thread_id,
            )
        raise HelperFailure(f"helper returned no Python stack for thread {thread_id}")

    def _attach_local_snapshots(
        self,
        frames: list[dict[str, Any]],
        *,
        process_id: int,
        thread_id: int,
    ) -> list[dict[str, Any]]:
        try:
            expected = frames[0] if frames else {}
            snapshots = read_python_locals(
                process_id,
                thread_id,
                expected_name=expected.get("name"),
                expected_path=expected.get("path"),
            )
        except LocalReadError as error:
            return [
                {
                    **frame,
                    "localsError": f"Python locals unavailable: {error}",
                }
                for frame in frames
            ]

        for index, frame in enumerate(frames):
            if index >= len(snapshots):
                frame["localsError"] = "Python locals were not found for this frame"
                continue
            snapshot = snapshots[index]
            if snapshot.name != frame["name"] or snapshot.path != frame["path"]:
                frame["localsError"] = "Python locals did not match the active frame"
                continue
            frame["locals"] = dict(snapshot.locals)
        return frames

    def _run_in_process_helper(
        self,
        process_id: int,
        timeout_ms: int,
    ) -> list[dict[str, Any]]:
        with self._lock:
            if self._in_process_circuit_open:
                raise InProcessUnwinderUnavailable(
                    "in-process unwinder circuit is open"
                )
            if self._in_process_worker_active:
                raise InProcessUnwinderUnavailable(
                    "in-process unwinder worker is already active"
                )
            self._in_process_worker_active = True

        results: Queue[tuple[bool, object]] = Queue(maxsize=1)

        def collect() -> None:
            try:
                stacks = [stack.to_dict() for stack in read_python_stacks(process_id)]
            except Exception as error:
                results.put((False, error))
            else:
                results.put((True, stacks))
            finally:
                with self._lock:
                    self._in_process_worker_active = False

        try:
            Thread(
                target=collect,
                name="pyrust-cpython-unwinder",
                daemon=True,
            ).start()
        except Exception as error:
            with self._lock:
                self._in_process_worker_active = False
            raise HelperFailure(
                f"could not start in-process unwinder worker: {error}"
            ) from error

        try:
            succeeded, value = results.get(timeout=timeout_ms / 1_000)
        except Empty as error:
            with self._lock:
                self._in_process_circuit_open = True
            raise InProcessHelperTimeout(
                f"in-process unwind exceeded {timeout_ms} ms"
            ) from error
        if succeeded:
            assert isinstance(value, list)
            return value
        if isinstance(value, StackReadError):
            raise value
        if isinstance(value, Exception):
            raise HelperFailure(f"in-process unwind failed: {value}") from value
        raise HelperFailure("in-process unwind failed without an error")

    @staticmethod
    def _run_external_helper(
        command: str,
        process_id: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        rendered = command.format(pid=process_id)
        argv = shlex.split(rendered)
        if not argv:
            raise HelperFailure("configured helper command is empty")
        environment = os.environ.copy()
        environment["PYRUST_TARGET_PID"] = str(process_id)
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                check=False,
                env=environment,
                text=True,
                timeout=timeout_ms / 1_000,
            )
        except subprocess.TimeoutExpired as error:
            raise HelperTimeout(f"exceeded {timeout_ms} ms") from error
        except OSError as error:
            raise HelperFailure(f"could not start configured helper: {error}") from error
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            suffix = f": {detail}" if detail else ""
            raise HelperFailure(
                f"command exited with status {result.returncode}{suffix}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise HelperFailure("helper output is not valid JSON") from error
        if not isinstance(payload, dict):
            raise HelperFailure("helper output is not a JSON object")
        if payload.get("ok") is False:
            helper_error = payload.get("error") or {}
            raise HelperFailure(
                str(helper_error.get("message", "helper reported failure"))
            )
        return payload

    @staticmethod
    def _validate_python_frame(frame: object) -> dict[str, Any]:
        if not isinstance(frame, dict):
            raise HelperFailure("helper frame is not an object")
        name = frame.get("name")
        path = frame.get("path")
        line = frame.get("line")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(path, str)
            or not path
            or not isinstance(line, int)
            or isinstance(line, bool)
            or line <= 0
        ):
            raise HelperFailure("helper frame is malformed")
        return {"name": name, "path": path, "line": line}

    @staticmethod
    def _merge_frames(
        native_frames: list[dict[str, Any]],
        python_frames: list[dict[str, Any]],
        thread_id: int,
        process_id: int | None,
        native_ids: list[int],
        context: ProxyContext,
        synthetic_epoch: int | None,
    ) -> list[dict[str, Any]]:
        insertion_index = MixedStackHooks._python_boundary_index(native_frames)
        if insertion_index is None or not python_frames:
            raise HelperFailure("native Python/Rust boundary was not found")

        synthetic: list[dict[str, Any]] = []
        for index, frame in enumerate(python_frames):
            frame_id = context.synthetic_frames.allocate(
                thread_id,
                (
                    index,
                    frame["name"],
                    frame["path"],
                    frame["line"],
                ),
                frame,
                native_frame_ids=native_ids,
                expected_epoch=synthetic_epoch,
                process_id=process_id,
            )
            synthetic.append(
                {
                    "id": frame_id,
                    "name": frame["name"],
                    "source": {
                        "name": Path(frame["path"]).name,
                        "path": frame["path"],
                    },
                    "line": frame["line"],
                    "column": 1,
                    "presentationHint": "normal",
                }
            )
        return (
            native_frames[:insertion_index]
            + synthetic
            + native_frames[insertion_index:]
        )

    @staticmethod
    def _python_boundary_index(
        native_frames: list[dict[str, Any]],
    ) -> int | None:
        """Locate the first PyO3/CPython bridge below native user callees."""

        for index, frame in enumerate(native_frames):
            if index == 0:
                continue
            name = str(frame.get("name", "")).lower()
            if any(
                marker in name
                for marker in (
                    "__pyfunction_",
                    "__pymethod_",
                    "pyo3::impl_::trampoline",
                    "_pyfunction_vectorcall",
                    "_pyeval_",
                    "pyeval_",
                )
            ):
                return index
        return None

    @staticmethod
    def _is_mixed_stack_candidate(
        native_frames: list[dict[str, Any]],
    ) -> bool:
        return MixedStackHooks._python_boundary_index(native_frames) is not None

    @staticmethod
    def _can_restore_debugpy_snapshot(
        native_frames: list[dict[str, Any]],
        python_frames: list[dict[str, Any]],
    ) -> bool:
        return (
            MixedStackHooks._python_boundary_index(native_frames) is not None
            and bool(python_frames)
        )

    @staticmethod
    def _native_stack_arguments(
        client_arguments: Mapping[str, Any],
        thread_id: int,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"threadId": thread_id}
        stack_format = client_arguments.get("format")
        if isinstance(stack_format, dict):
            arguments["format"] = dict(stack_format)
        return arguments

    @staticmethod
    def _require_epoch(
        context: ProxyContext,
        expected_epoch: int,
        *,
        process_id: int | None = None,
        thread_id: int | None = None,
    ) -> None:
        if process_id is not None:
            snapshot = context.state.process_snapshot(process_id)
            if (
                snapshot is None
                or not snapshot.is_stopped
                or (thread_id is not None and not context.state.is_thread_stopped(thread_id))
            ):
                raise StaleStopError(
                    "debuggee continued while stackTrace was being collected"
                )
            return
        if not context.state.is_stopped or context.state.stop_epoch != expected_epoch:
            raise StaleStopError(
                "debuggee continued while stackTrace was being collected"
            )

    def _diagnose_once(self, context: ProxyContext, message: str) -> None:
        epoch = context.state.stop_epoch
        with self._lock:
            if epoch in self._diagnosed_epochs:
                return
            self._diagnosed_epochs.add(epoch)
        context.send_output(f"{message}\n", category="stderr")

    def _diagnose_in_process_timeout_once(self, context: ProxyContext) -> None:
        with self._lock:
            if self._in_process_timeout_diagnosed:
                return
            self._in_process_timeout_diagnosed = True
        context.send_output(
            "PyRust in-process unwinder timeout; circuit opened for this session\n",
            category="stderr",
        )

    def close(self) -> None:
        with self._lock:
            manager = self._process_manager
            self._process_manager = None
            python_manager = self._python_manager
            self._python_manager = None
            python_debug_registry = self._python_debug_registry
            self._python_debug_registry = None
            child_only_process = self._child_only_process
            self._child_only_process = None
            self._child_only_mode = False
        if manager is not None:
            manager.close()
        if python_manager is not None:
            python_manager.close()
        if python_debug_registry is not None:
            python_debug_registry.cleanup()
        if child_only_process is not None:
            _terminate_process(child_only_process)

    def _configure_python_debug(
        self,
        arguments: dict[str, Any],
        *,
        child_registry: str | None,
        configured_registry: str | None,
    ) -> Path:
        if configured_registry is not None:
            registry = Path(configured_registry)
        elif child_registry is not None:
            registry = Path(child_registry) / "debugpy"
        else:
            self._python_debug_registry = TemporaryDirectory(
                prefix="pyrust-debugpy-",
            )
            registry = Path(self._python_debug_registry.name)
        registry.mkdir(parents=True, exist_ok=True)

        launch_env = arguments.get("env", {})
        if not isinstance(launch_env, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in launch_env.items()
        ):
            raise ValueError("launch env must be string pairs")
        python_bootstrap = str(Path(__file__).resolve().parents[1] / "python")
        debugpy_module = importlib.import_module("debugpy")
        if not debugpy_module.__file__:
            raise ValueError("installed debugpy package has no import path")
        debugpy_site_packages = str(Path(debugpy_module.__file__).resolve().parents[1])
        inherited_python_path = launch_env.get("PYTHONPATH", os.environ.get("PYTHONPATH"))
        launch_env = dict(launch_env)
        launch_env.update(
            {
                "PYRUST_DEBUGPY_ENABLE": "1",
                "PYRUST_DEBUGPY_REGISTRY": str(registry),
                "PYRUST_DEBUGPY_WAIT_FOR_CLIENT": "1",
                "PYRUST_DEBUGPY_PYTHON": sys.executable,
                "PYDEVD_DISABLE_FILE_VALIDATION": "1",
                "PYTHONPATH": (
                    f"{python_bootstrap}{os.pathsep}{debugpy_site_packages}"
                    if not inherited_python_path
                    else (
                        f"{python_bootstrap}{os.pathsep}{debugpy_site_packages}"
                        f"{os.pathsep}{inherited_python_path}"
                    )
                ),
            }
        )
        arguments["env"] = launch_env
        return registry

    def _start_child_only_parent(
        self,
        arguments: Mapping[str, Any],
        context: ProxyContext,
    ) -> subprocess.Popen[bytes]:
        program = arguments.get("program")
        if not isinstance(program, str) or not program:
            raise ValueError("child coordinator launch requires a program")
        raw_args = arguments.get("args", [])
        if not isinstance(raw_args, list) or not all(
            isinstance(item, str) for item in raw_args
        ):
            raise ValueError("child coordinator launch args must be strings")
        cwd = arguments.get("cwd", str(Path.cwd()))
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("child coordinator launch cwd must be a string")
        environment = os.environ.copy()
        launch_env = arguments.get("env", {})
        if not isinstance(launch_env, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in launch_env.items()
        ):
            raise ValueError("child coordinator launch env must be string pairs")
        environment.update(launch_env)
        try:
            process = subprocess.Popen(
                [program, *raw_args],
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise HelperFailure(
                f"could not launch child coordinator parent: {error}"
            ) from error

        def wait_for_parent() -> None:
            exit_code = process.wait()
            context.state.on_terminated(
                {"body": {"systemProcessId": process.pid}}
            )
            context.send_event("exited", {"exitCode": exit_code})
            context.send_event(
                "terminated",
                {"systemProcessId": process.pid},
            )

        Thread(
            target=wait_for_parent,
            name="pyrust-child-parent-waiter",
            daemon=True,
        ).start()
        return process

    def _terminate_child_only_process(self) -> None:
        process = self._child_only_process
        self._child_only_process = None
        if process is not None:
            _terminate_process(process)

    def _emit_child_event(
        self,
        context: ProxyContext,
        event: str,
        body: Mapping[str, Any],
    ) -> None:
        message: Message = {"body": dict(body)}
        if event == "stopped":
            if self.on_stopped(message, context) is None:
                return
        elif event == "continued":
            self.on_continued(message, context)
        context.send_event(event, body)

    def _emit_python_event(
        self,
        context: ProxyContext,
        event: str,
        body: Mapping[str, Any],
    ) -> None:
        if event == "stopped":
            process_id = body.get("systemProcessId")
            with self._lock:
                python_step_in = (
                    isinstance(process_id, int)
                    and process_id in self._python_step_in_suppress_python
                )
            if python_step_in:
                thread_id = body.get("threadId")
                manager = self._python_manager
                if manager is not None and manager.owns_thread(thread_id):
                    assert isinstance(thread_id, int)
                    try:
                        manager.continue_thread(thread_id, single_thread=True)
                    except PythonTransportError as error:
                        with self._lock:
                            self._python_step_in_processes.discard(process_id)
                            self._python_step_in_suppress_python.discard(process_id)
                        context.send_output(
                            f"PyRust Python-to-Rust step failed: {error}\n",
                            category="stderr",
                        )
                return
            if isinstance(process_id, int):
                with self._lock:
                    self._python_handoffs = {
                        key for key in self._python_handoffs if key[0] != process_id
                    }
            self._restore_function_breakpoints(context)
        elif event == "continued":
            process_id = body.get("systemProcessId")
            if isinstance(process_id, int):
                with self._lock:
                    self._native_active.pop(process_id, None)
                    self._native_lease_frames = {
                        frame_id: route
                        for frame_id, route in self._native_lease_frames.items()
                        if route.process_id != process_id
                    }
        context.send_event(event, body)

    def _is_python_handoff_process(self, process_id: int) -> bool:
        with self._lock:
            return any(key[0] == process_id for key in self._python_handoffs)

    @staticmethod
    def _continue_child_handoff(
        manager: ProcessManager,
        thread_id: int,
        process_id: int,
    ) -> None:
        del process_id
        try:
            manager.continue_thread(thread_id, single_thread=True)
        except ChildTransportError:
            pass

    def _handoff_snapshot_to_debugpy(
        self,
        frame_id: object,
        context: ProxyContext,
    ) -> LocalResponse | None:
        if not isinstance(frame_id, int):
            return None
        synthetic = context.synthetic_frames.get(frame_id)
        manager = self._python_manager
        if synthetic is None or manager is None or synthetic.process_id is None:
            return None
        key = (synthetic.process_id, synthetic.thread_id)
        frame = synthetic.value
        path = frame.get("path") if isinstance(frame, dict) else None
        line = frame.get("line") if isinstance(frame, dict) else None
        if not isinstance(path, str) or not isinstance(line, int):
            return LocalResponse(
                success=False,
                message="Python frame has no source location for debugpy handoff",
            )
        with self._lock:
            if key in self._python_handoffs:
                return LocalResponse(
                    success=False,
                    message=(
                        "Python frame handoff to debugpy is in progress; "
                        "wait for the next Python stop"
                    ),
                )
            self._python_handoffs.add(key)
        try:
            name = frame.get("name") if isinstance(frame, dict) else None
            if not isinstance(name, str) or not name:
                raise PythonTransportError(
                    "Python frame has no function name for debugpy handoff"
                )
            manager.arm_targeted_handoff(
                synthetic.process_id,
                native_thread_id=synthetic.thread_id,
                target_name=name,
                target_path=path,
            )
            context.send_output(
                "PyRust is switching this process from CodeLLDB to debugpy; "
                "Python evaluation will be live at the next Python stop.\n",
                category="console",
            )
            child_manager = self._process_manager
            if (
                child_manager is not None
                and child_manager.owns_thread(synthetic.thread_id)
            ):
                child_manager.continue_thread(
                    synthetic.thread_id,
                    single_thread=True,
                )
            else:
                response = context.request_downstream(
                    "continue",
                    {
                        "threadId": synthetic.thread_id,
                        "singleThread": True,
                    },
                    timeout=10,
                )
                if response.get("success") is not True:
                    raise PythonTransportError(
                        str(response.get("message", "native handoff continue failed"))
                    )
                context.state.on_continued(
                    {
                        "body": {
                            "threadId": synthetic.thread_id,
                            "systemProcessId": synthetic.process_id,
                        }
                    }
                )
        except (ChildTransportError, PythonTransportError, Exception) as error:
            with self._lock:
                self._python_handoffs.discard(key)
            return LocalResponse(
                success=False,
                message=f"could not hand Python frame to debugpy: {error}",
            )
        return LocalResponse(body={"scopes": []})

    def _capture_native_lease_frames(
        self,
        manager: PythonProcessManager,
        python_thread_id: int,
        context: ProxyContext,
    ) -> list[dict[str, Any]]:
        process_id, native_thread_id = manager.native_identity_for_thread(
            python_thread_id
        )
        stopped = Event()
        with self._lock:
            self._native_maintenance[process_id] = stopped
        child_manager = self._process_manager
        try:
            if child_manager is not None and child_manager.owns_thread(native_thread_id):
                child_manager.pause_thread(native_thread_id)
            else:
                context.request_downstream(
                    "pause",
                    {"threadId": native_thread_id},
                    timeout=10,
                )
            if not stopped.wait(10):
                raise TimeoutError("native maintenance pause did not stop")
            response = (
                child_manager.stack_trace(native_thread_id, {"levels": 200})
                if child_manager is not None
                and child_manager.owns_thread(native_thread_id)
                else context.request_downstream(
                    "stackTrace",
                    {"threadId": native_thread_id, "startFrame": 0, "levels": 200},
                    timeout=10,
                )
            )
            frames = (response.get("body") or {}).get("stackFrames")
            if not isinstance(frames, list):
                raise PythonTransportError("native maintenance stack is malformed")
            descriptors = self._publish_native_lease_frames(
                process_id,
                native_thread_id,
                python_thread_id,
                frames,
            )
            continued = Event()
            with self._lock:
                self._native_suppress_continued[process_id] = continued
            if child_manager is not None and child_manager.owns_thread(native_thread_id):
                child_manager.continue_thread(native_thread_id, single_thread=True)
            else:
                context.request_downstream(
                    "continue",
                    {"threadId": native_thread_id, "singleThread": True},
                    timeout=10,
                )
            continued.wait(5)
            context.state.on_stopped(
                {
                    "body": {
                        "threadId": python_thread_id,
                        "systemProcessId": process_id,
                        "allThreadsStopped": True,
                    }
                },
                owner="python",
            )
            return descriptors
        finally:
            with self._lock:
                self._native_maintenance.pop(process_id, None)

    def _publish_native_lease_frames(
        self,
        process_id: int,
        native_thread_id: int,
        python_thread_id: int,
        frames: list[object],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        with self._lock:
            self._native_lease_frames = {
                frame_id: route
                for frame_id, route in self._native_lease_frames.items()
                if route.python_thread_id != python_thread_id
            }
            for frame in frames:
                if not isinstance(frame, dict):
                    continue
                source = frame.get("source")
                path = source.get("path") if isinstance(source, dict) else None
                line = frame.get("line")
                name = frame.get("name")
                instruction_pointer = frame.get("instructionPointerReference")
                if (
                    not isinstance(path, str)
                    or not path.endswith(".rs")
                    or not _path_is_within(path, self._source_root)
                    or not isinstance(line, int)
                    or not isinstance(name, str)
                    or not isinstance(instruction_pointer, str)
                    or not instruction_pointer
                ):
                    continue
                frame_id = self._native_lease_next
                self._native_lease_next += 1
                self._native_lease_frames[frame_id] = _NativeLeaseFrame(
                    process_id=process_id,
                    native_thread_id=native_thread_id,
                    python_thread_id=python_thread_id,
                    name=name,
                    path=path,
                    line=line,
                    instruction_pointer_reference=instruction_pointer,
                )
                self._native_lease_issued.add(frame_id)
                result.append(
                    {
                        "id": frame_id,
                        "name": name,
                        "source": {"name": Path(path).name, "path": path},
                        "line": line,
                        "column": 1,
                        "instructionPointerReference": instruction_pointer,
                        "presentationHint": "normal",
                    }
                )
        return result

    def _acquire_native_lease(
        self,
        route: _NativeLeaseFrame,
        context: ProxyContext,
    ) -> int:
        with self._lock:
            active = self._native_active.get(route.process_id)
            if active is not None and route.frame_id is not None:
                return route.frame_id
            stopped = None if active is not None else Event()
            if stopped is not None:
                self._native_maintenance[route.process_id] = stopped
        manager = self._process_manager
        try:
            if stopped is not None:
                if manager is not None and manager.owns_thread(route.native_thread_id):
                    manager.pause_thread(route.native_thread_id)
                else:
                    context.request_downstream(
                        "pause",
                        {"threadId": route.native_thread_id},
                        timeout=10,
                    )
                if not stopped.wait(10):
                    raise TimeoutError("CodeLLDB did not acquire the Rust frame lease")
            response = (
                manager.stack_trace(route.native_thread_id, {"levels": 200})
                if manager is not None and manager.owns_thread(route.native_thread_id)
                else context.request_downstream(
                    "stackTrace",
                    {
                        "threadId": route.native_thread_id,
                        "startFrame": 0,
                        "levels": 200,
                    },
                    timeout=10,
                )
            )
            frames = (response.get("body") or {}).get("stackFrames")
            if not isinstance(frames, list):
                raise PythonTransportError("CodeLLDB returned no Rust stack")
            fresh = next(
                (
                    frame
                    for frame in frames
                    if isinstance(frame, dict)
                    and frame.get("name") == route.name
                    and isinstance(frame.get("source"), dict)
                    and frame["source"].get("path") == route.path
                ),
                None,
            )
            frame_id = fresh.get("id") if isinstance(fresh, dict) else None
            if not isinstance(frame_id, int):
                raise PythonTransportError(
                    f"CodeLLDB could not resolve Rust frame {route.name}"
                )
            with self._lock:
                route.frame_id = frame_id
                self._native_active[route.process_id] = (
                    route.native_thread_id,
                    route.python_thread_id,
                )
            return frame_id
        finally:
            if stopped is not None:
                with self._lock:
                    self._native_maintenance.pop(route.process_id, None)

    def _release_native_lease(
        self,
        process_id: int,
        context: ProxyContext,
    ) -> None:
        with self._lock:
            active = self._native_active.pop(process_id, None)
        if active is None:
            return
        native_thread_id, python_thread_id = active
        continued = Event()
        with self._lock:
            self._native_suppress_continued[process_id] = continued
            for route in self._native_lease_frames.values():
                if route.process_id == process_id:
                    route.frame_id = None
        manager = self._process_manager
        if manager is not None and manager.owns_thread(native_thread_id):
            manager.continue_thread(native_thread_id, single_thread=True)
        else:
            context.request_downstream(
                "continue",
                {"threadId": native_thread_id, "singleThread": True},
                timeout=10,
            )
        continued.wait(5)
        context.state.on_stopped(
            {
                "body": {
                    "threadId": python_thread_id,
                    "systemProcessId": process_id,
                    "allThreadsStopped": True,
                }
            },
            owner="python",
        )

    def _step_from_native_lease(
        self,
        route: _NativeLeaseFrame,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse:
        python_manager = self._python_manager
        if python_manager is None:
            return LocalResponse(
                success=False,
                message="debugpy is unavailable for the Rust-frame handoff",
            )
        try:
            self._acquire_native_lease(route, context)
            with self._lock:
                user_breakpoints = [
                    dict(item) for item in self._instruction_breakpoints
                ]
            temporary = {
                "instructionReference": route.instruction_pointer_reference
            }
            breakpoints = [*user_breakpoints, temporary]
            manager = self._process_manager
            response = (
                manager.set_instruction_breakpoints(
                    route.process_id,
                    breakpoints,
                )
                if manager is not None
                and manager.owns_thread(route.native_thread_id)
                else context.request_downstream(
                    "setInstructionBreakpoints",
                    {"breakpoints": breakpoints},
                    timeout=10,
                )
            )
            if response.get("success") is not True:
                return LocalResponse(
                    success=False,
                    message=str(
                        response.get(
                            "message",
                            "CodeLLDB rejected the Rust return breakpoint",
                        )
                    ),
                )
            command = str(request.get("command"))
            arguments = {
                key: value
                for key, value in dict(request.get("arguments") or {}).items()
                if key in {"granularity", "singleThread", "targetId"}
            }
            with self._lock:
                self._pending_native_step = _NativeLeaseStep(
                    process_id=route.process_id,
                    thread_id=route.native_thread_id,
                    command=command,
                    arguments=arguments,
                )
            self._release_native_lease(route.process_id, context)
            resumed = python_manager.continue_thread(
                route.python_thread_id,
                single_thread=True,
            )
            return LocalResponse(body=dict(resumed.get("body") or {}))
        except (ChildTransportError, PythonTransportError, TimeoutError) as error:
            self._restore_instruction_breakpoints(route.process_id, context)
            return LocalResponse(success=False, message=str(error))

    def _restore_instruction_breakpoints(
        self,
        process_id: int | None,
        context: ProxyContext,
    ) -> _NativeLeaseStep | None:
        with self._lock:
            pending = self._pending_native_step
            if (
                pending is None
                or process_id != pending.process_id
            ):
                return None
            self._pending_native_step = None
            breakpoints = [
                dict(item) for item in self._instruction_breakpoints
            ]
        try:
            manager = self._process_manager
            if manager is not None and manager.owns_process(pending.process_id):
                manager.set_instruction_breakpoints(
                    pending.process_id,
                    breakpoints,
                )
            else:
                context.request_downstream(
                    "setInstructionBreakpoints",
                    {"breakpoints": breakpoints},
                    timeout=10,
                )
        except Exception:
            pass
        return pending

    def _continue_native_lease_step(
        self,
        step: _NativeLeaseStep,
        context: ProxyContext,
    ) -> None:
        try:
            manager = self._process_manager
            if manager is not None and manager.owns_thread(step.thread_id):
                response = manager.step_thread(
                    step.command,
                    step.thread_id,
                    step.arguments,
                )
            else:
                response = context.request_downstream(
                    step.command,
                    {**step.arguments, "threadId": step.thread_id},
                    timeout=10,
                )
        except Exception as error:
            raise ChildTransportError(
                f"CodeLLDB {step.command} request failed: {error}"
            ) from error
        if response.get("success") is not True:
            raise ChildTransportError(
                str(response.get("message", f"CodeLLDB {step.command} failed"))
            )

    def _prepare_native_step_target(
        self,
        manager: PythonProcessManager,
        thread_id: int,
        context: ProxyContext,
    ) -> None:
        try:
            target = manager.direct_native_step_target(thread_id)
        except PythonTransportError:
            return
        if target is None:
            return
        with self._lock:
            user_breakpoints = [dict(item) for item in self._function_breakpoints]
            if any(item.get("name") == target for item in user_breakpoints):
                return
            self._temporary_native_step_target = target
        try:
            response = context.request_downstream(
                "setFunctionBreakpoints",
                {"breakpoints": [*user_breakpoints, {"name": target}]},
                timeout=10,
            )
            if response.get("success") is not True:
                with self._lock:
                    self._temporary_native_step_target = None
        except Exception:
            with self._lock:
                self._temporary_native_step_target = None

    def _restore_function_breakpoints(self, context: ProxyContext) -> None:
        with self._lock:
            if self._temporary_native_step_target is None:
                return
            self._temporary_native_step_target = None
            breakpoints = [dict(item) for item in self._function_breakpoints]
        try:
            context.request_downstream(
                "setFunctionBreakpoints",
                {"breakpoints": breakpoints},
                timeout=10,
            )
        except Exception:
            pass

    @staticmethod
    def _current_python_frame_id(
        frame_id: object,
        context: ProxyContext,
    ) -> dict[str, Any] | None:
        if not isinstance(frame_id, int):
            return None
        synthetic = context.synthetic_frames.get(frame_id)
        if synthetic is None or not isinstance(synthetic.value, dict):
            return None
        return synthetic.value

    def _console_frame_id_for(self, request: Message) -> int | None:
        frame_id = (request.get("arguments") or {}).get("frameId")
        if isinstance(frame_id, int) and not isinstance(frame_id, bool):
            return frame_id
        with self._lock:
            return self._console_frame_id


def _dap_variable(name: str, value: object) -> dict[str, Any]:
    return {
        "name": name,
        "value": _display_python_value(value),
        "type": _python_type_name(value),
        "variablesReference": 0,
        "evaluateName": name,
    }


def _path_is_within(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _display_python_value(value: object) -> str:
    if hasattr(value, "type_name"):
        return f"<{getattr(value, 'type_name')}>"
    return repr(value)


def _python_type_name(value: object) -> str:
    if hasattr(value, "type_name"):
        return str(getattr(value, "type_name"))
    return type(value).__name__


def _evaluate_python_expression(
    expression: str,
    locals_snapshot: Mapping[str, object],
) -> object:
    try:
        parsed = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise PythonExpressionError(f"invalid Python expression: {error.msg}") from error

    def evaluate(node: ast.AST) -> object:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (str, int, float, bool, bytes)) or node.value is None:
                return node.value
        elif isinstance(node, ast.Name):
            if node.id not in locals_snapshot:
                raise PythonExpressionError(f"name {node.id!r} is not available")
            value = locals_snapshot[node.id]
            if hasattr(value, "type_name"):
                raise PythonExpressionError(
                    f"name {node.id!r} has unsupported type "
                    f"{getattr(value, 'type_name')!r}"
                )
            return value
        elif isinstance(node, ast.UnaryOp) and isinstance(
            node.op,
            (ast.UAdd, ast.USub, ast.Not),
        ):
            value = evaluate(node.operand)
            try:
                if isinstance(node.op, ast.UAdd):
                    return +value  # type: ignore[operator]
                if isinstance(node.op, ast.USub):
                    return -value  # type: ignore[operator]
                return not value
            except TypeError as error:
                raise PythonExpressionError(str(error)) from error
        elif isinstance(node, ast.BinOp) and isinstance(
            node.op,
            (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod),
        ):
            left = evaluate(node.left)
            right = evaluate(node.right)
            try:
                if isinstance(node.op, ast.Add):
                    return left + right  # type: ignore[operator]
                if isinstance(node.op, ast.Sub):
                    return left - right  # type: ignore[operator]
                if isinstance(node.op, ast.Mult):
                    return left * right  # type: ignore[operator]
                if isinstance(node.op, ast.Div):
                    return left / right  # type: ignore[operator]
                if isinstance(node.op, ast.FloorDiv):
                    return left // right  # type: ignore[operator]
                return left % right  # type: ignore[operator]
            except (ArithmeticError, TypeError) as error:
                raise PythonExpressionError(str(error)) from error
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            result = evaluate(node.values[0])
            for value in node.values[1:]:
                if isinstance(node.op, ast.And):
                    if not result:
                        return result
                elif result:
                    return result
                result = evaluate(value)
            return result
        elif isinstance(node, ast.Compare):
            left = evaluate(node.left)
            for operator, comparator in zip(node.ops, node.comparators, strict=True):
                right = evaluate(comparator)
                try:
                    if isinstance(operator, ast.Eq):
                        matches = left == right
                    elif isinstance(operator, ast.NotEq):
                        matches = left != right
                    elif isinstance(operator, ast.Lt):
                        matches = left < right
                    elif isinstance(operator, ast.LtE):
                        matches = left <= right
                    elif isinstance(operator, ast.Gt):
                        matches = left > right
                    elif isinstance(operator, ast.GtE):
                        matches = left >= right
                    else:
                        raise PythonExpressionError(
                            "comparison operator is not supported"
                        )
                except TypeError as error:
                    raise PythonExpressionError(str(error)) from error
                if not matches:
                    return False
                left = right
            return True
        raise PythonExpressionError(
            f"Python expression node {type(node).__name__} is not supported"
        )

    result = evaluate(parsed.body)
    if hasattr(result, "type_name") or not isinstance(
        result,
        (str, int, float, bool, bytes, type(None)),
    ):
        raise PythonExpressionError("expression result has an unsupported type")
    return result


def _child_codelldb_command() -> list[str]:
    adapter = os.environ.get("PYRUST_CODELLDB")
    liblldb = os.environ.get("PYRUST_LIBLLDB")
    if adapter and liblldb:
        return [adapter, "--liblldb", liblldb]
    candidates = sorted(
        (Path.home() / ".vscode-server" / "extensions").glob(
            "vadimcn.vscode-lldb-1.12.2*"
        )
    )
    for extension in reversed(candidates):
        candidate_adapter = extension / "adapter" / "codelldb"
        candidate_liblldb = extension / "lldb" / "lib" / "liblldb.so"
        if candidate_adapter.is_file() and candidate_liblldb.is_file():
            return [str(candidate_adapter), "--liblldb", str(candidate_liblldb)]
    raise ValueError("CodeLLDB 1.12.2 is required for child process debugging")


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _launch_process_metadata(
    program: str,
    raw_args: object = None,
) -> tuple[str, str, str]:
    """Infer the direct launch language without inspecting child process names."""

    executable = Path(program).name.lower()
    language = "Python" if executable.startswith("python") else "Rust"
    role = f"{language} process"
    args = (
        raw_args
        if isinstance(raw_args, list)
        and all(isinstance(item, str) for item in raw_args)
        else []
    )
    return role, role, shlex.join([program, *args])


def _prepare_child_registry(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for pattern in (
        "child-*.json",
        "ready-*",
        "attached-*",
        "complete-*",
        "workers-ready-*",
        "release",
    ):
        for candidate in path.glob(pattern):
            if candidate.is_file() or candidate.is_symlink():
                candidate.unlink()
    debugpy_registry = path / "debugpy"
    if debugpy_registry.is_dir():
        for pattern in ("debugpy-*.json", "debugpy-*.failed", "debugpy-*.ready"):
            for candidate in debugpy_registry.glob(pattern):
                candidate.unlink(missing_ok=True)
