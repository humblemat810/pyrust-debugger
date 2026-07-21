"""Fixture-bound Python/Rust stack augmentation for the first workable slice."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from queue import Empty, Queue
import shlex
import subprocess
from threading import Lock, Thread
from typing import Any, Mapping

from .child_transport import ChildTransportError
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


class MixedStackHooks(ProxyHooks):
    """Merge one CPython stack into either supported fixture stack."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._helper_command: str | None = None
        self._helper_timeout_ms = 1_000
        self._stopped_thread_id: int | None = None
        self._diagnosed_epochs: set[int] = set()
        self._in_process_worker_active = False
        self._in_process_circuit_open = False
        self._in_process_timeout_diagnosed = False
        self._process_manager: ProcessManager | None = None
        self._child_only_process: subprocess.Popen[bytes] | None = None
        self._child_only_mode = False

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
            self._diagnosed_epochs.clear()
            self._child_only_mode = process_mode == "children"
            if process_metadata is not None:
                context.state.set_default_process_metadata(*process_metadata)
            if self._process_manager is not None:
                self._process_manager.close()
                self._process_manager = None
            self._terminate_child_only_process()
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
        outgoing["arguments"] = arguments
        return outgoing

    def on_stopped(self, event: Message, context: ProxyContext) -> Message:
        thread_id = (event.get("body") or {}).get("threadId")
        with self._lock:
            self._stopped_thread_id = (
                thread_id
                if isinstance(thread_id, int)
                and not isinstance(thread_id, bool)
                and thread_id > 0
                else None
            )
        return event

    def on_continued(self, event: Message, context: ProxyContext) -> Message:
        with self._lock:
            self._stopped_thread_id = None
        return event

    def on_threads(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        del request, context
        if self._process_manager is None:
            return None
        return LocalResponse(body={"threads": self._process_manager.threads()})

    def on_continue_request(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        thread_id = (request.get("arguments") or {}).get("threadId")
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

    def on_set_breakpoints(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
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

    def on_configuration_done(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
        del request, context
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
        manager = self._process_manager
        if manager is not None and manager.native_frame_route(frame_id) is not None:
            assert isinstance(frame_id, int)
            try:
                response = manager.scopes(frame_id)
            except ChildTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        frame = self._current_python_frame(request, context)
        if frame is None:
            return None
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
        frame_id = (request.get("arguments") or {}).get("frameId")
        manager = self._process_manager
        if manager is not None and manager.native_frame_route(frame_id) is not None:
            assert isinstance(frame_id, int)
            try:
                response = manager.evaluate(frame_id, request.get("arguments") or {})
            except ChildTransportError as error:
                return LocalResponse(success=False, message=str(error))
            return LocalResponse(body=dict(response.get("body") or {}))
        frame = self._current_python_frame(request, context)
        if frame is None:
            return None
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

        try:
            self._require_epoch(
                context,
                request_epoch,
                process_id=child_process,
                thread_id=thread_id,
            )
            python_frames = self._read_python_frames(
                process_id=(
                    context.state.process_id_for_thread(thread_id)
                    or self._fallback_process_id(thread_id)
                ),
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

        start = arguments.get("startFrame", 0)
        levels = arguments.get("levels")
        start = start if isinstance(start, int) and start >= 0 else 0
        if isinstance(levels, int) and levels > 0:
            page = merged[start : start + levels]
        else:
            page = merged[start:]
        return LocalResponse(
            body={"stackFrames": page, "totalFrames": len(merged)}
        )

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
            snapshots = read_python_locals(process_id, thread_id)
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
        insertion_index = MixedStackHooks._fixture_insertion_index(
            native_frames,
            python_frames,
        )

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
    def _fixture_insertion_index(
        native_frames: list[dict[str, Any]],
        python_frames: list[dict[str, Any]],
    ) -> int:
        names = [str(frame.get("name", "")) for frame in native_frames]
        leaves = [MixedStackHooks._frame_leaf(frame) for frame in native_frames]
        if leaves[:2] == ["rust_inner", "rust_outer"]:
            return 2

        if leaves[:1] != ["rust_callback"]:
            raise HelperFailure(
                f"native fixture boundary was not found: {leaves[:2]!r}"
            )
        python_names = [str(frame.get("name", "")) for frame in python_frames]
        if python_names[:2] != ["python_inner", "python_outer"]:
            raise HelperFailure(
                f"Rust-outer Python boundary was not found: {python_names!r}"
            )
        try:
            rust_outer_index = next(
                index
                for index, name in enumerate(names[1:], start=1)
                if "rust_outer" in name
            )
        except ValueError as error:
            raise HelperFailure(
                "Rust-outer fixture boundary was not found"
            ) from error
        if not any(
            name.endswith("::main") or "::main::" in name
            for name in names[rust_outer_index + 1 :]
        ):
            raise HelperFailure("Rust-outer fixture entry boundary was not found")
        return 1

    @staticmethod
    def _frame_leaf(frame: Mapping[str, Any]) -> str:
        return str(frame.get("name", "")).rsplit("::", 1)[-1]

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
            child_only_process = self._child_only_process
            self._child_only_process = None
            self._child_only_mode = False
        if manager is not None:
            manager.close()
        if child_only_process is not None:
            _terminate_process(child_only_process)

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
            self.on_stopped(message, context)
        elif event == "continued":
            self.on_continued(message, context)
        context.send_event(event, body)

    @staticmethod
    def _current_python_frame(
        request: Message,
        context: ProxyContext,
    ) -> dict[str, Any] | None:
        frame_id = (request.get("arguments") or {}).get("frameId")
        if not isinstance(frame_id, int):
            return None
        synthetic = context.synthetic_frames.get(frame_id)
        if synthetic is None or not isinstance(synthetic.value, dict):
            return None
        return synthetic.value


def _dap_variable(name: str, value: object) -> dict[str, Any]:
    return {
        "name": name,
        "value": _display_python_value(value),
        "type": _python_type_name(value),
        "variablesReference": 0,
        "evaluateName": name,
    }


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
    for pattern in ("child-*.json", "ready-*", "attached-*", "release"):
        for candidate in path.glob(pattern):
            if candidate.is_file() or candidate.is_symlink():
                candidate.unlink()
