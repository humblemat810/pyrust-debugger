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

    def on_launch(self, request: Message, context: ProxyContext) -> Message:
        outgoing = dict(request)
        arguments = dict(outgoing.get("arguments") or {})
        helper_command = arguments.pop("pyrustHelperCommand", None)
        helper_timeout_ms = arguments.pop("pyrustHelperTimeoutMs", None)

        if helper_command is not None and not isinstance(helper_command, str):
            raise ValueError("pyrustHelperCommand must be a string")
        if helper_timeout_ms is not None and (
            not isinstance(helper_timeout_ms, int)
            or isinstance(helper_timeout_ms, bool)
            or helper_timeout_ms <= 0
        ):
            raise ValueError("pyrustHelperTimeoutMs must be a positive integer")

        with self._lock:
            self._helper_command = helper_command
            self._helper_timeout_ms = helper_timeout_ms or 1_000
            self._stopped_thread_id = None
            self._diagnosed_epochs.clear()
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

    def on_scopes(
        self,
        request: Message,
        context: ProxyContext,
    ) -> LocalResponse | None:
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

        request_epoch = context.state.stop_epoch
        try:
            self._require_epoch(context, request_epoch)
        except StaleStopError as error:
            return LocalResponse(success=False, message=str(error))
        native_response = context.request_downstream(
            "stackTrace",
            self._native_stack_arguments(arguments, thread_id),
            timeout=10,
        )
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
            self._require_epoch(context, request_epoch)
            python_frames = self._read_python_frames(
                process_id=context.state.process_id or self._fallback_process_id(thread_id),
                thread_id=thread_id,
            )
            self._require_epoch(context, request_epoch)
            merged = self._merge_frames(
                native_frames,
                python_frames,
                thread_id,
                native_ids,
                context,
                request_epoch,
            )
            self._require_epoch(context, request_epoch)
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
                self._require_epoch(context, request_epoch)
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
        with self._lock:
            stopped_thread_id = self._stopped_thread_id
        # In the fixed single-thread Linux fixture, CodeLLDB's DAP thread ID is
        # the OS TID and equals the process ID.
        return stopped_thread_id or thread_id

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
        native_ids: list[int],
        context: ProxyContext,
        request_epoch: int,
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
                expected_epoch=request_epoch,
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

        if names[:1] != ["rust_outer_python_inner::rust_callback"]:
            raise HelperFailure(
                f"native fixture boundary was not found: {leaves[:2]!r}"
            )
        python_names = [str(frame.get("name", "")) for frame in python_frames]
        if python_names not in (
            ["python_inner", "python_outer"],
            ["python_inner", "python_outer", "<module>"],
        ):
            raise HelperFailure(
                f"Rust-outer Python boundary was not found: {python_names!r}"
            )
        try:
            rust_outer_index = names.index(
                "rust_outer_python_inner::rust_outer",
                1,
            )
            names.index(
                "rust_outer_python_inner::main",
                rust_outer_index + 1,
            )
        except ValueError as error:
            raise HelperFailure(
                "Rust-outer fixture boundary was not found"
            ) from error
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
    def _require_epoch(context: ProxyContext, expected_epoch: int) -> None:
        if (
            not context.state.is_stopped
            or context.state.stop_epoch != expected_epoch
        ):
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
