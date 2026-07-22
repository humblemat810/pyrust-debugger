"""Interpreter-local live frame service for CPython subinterpreters.

The service is entered through CPython 3.14's remote-debug script protocol and
runs synchronously on the selected Python thread. It deliberately starts no
threads and imports no debugpy state, which keeps it safe for isolated
subinterpreters.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time
from types import FrameType
from typing import Any, Callable, Mapping


class LiveLeaseError(RuntimeError):
    """The selected live Python frame cannot be serviced."""


_INTERPRETERS_EXEC_HOOK_INSTALLED = False


def run_live_lease(
    directory: str,
    expected_name: str,
    expected_path: str,
) -> None:
    """Serve requests against one exact live frame until released."""

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    frame = _find_frame(expected_name, expected_path)
    service = _LiveFrameService(root, frame, generation=1)
    service.run()


def install_subinterpreter_breakpoints(directory: str) -> None:
    """Install a threadless source-breakpoint tracer in this interpreter."""

    if isinstance(sys.gettrace(), _BreakpointTracer):
        return
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    tracer = _BreakpointTracer(root)
    sys.settrace(tracer)
    threading.settrace(tracer)


def install_subinterpreter_exec_hook(directory: str) -> None:
    """Install tracing on the exact thread used by ``_interpreters.exec``."""

    global _INTERPRETERS_EXEC_HOOK_INSTALLED
    if _INTERPRETERS_EXEC_HOOK_INSTALLED:
        return
    try:
        import _interpreters
    except ImportError:
        return
    original_exec = _interpreters.exec
    bootstrap = (
        "from pyrust_stack.live_lease import "
        "install_subinterpreter_breakpoints as _pyrust_install_breakpoints\n"
        f"_pyrust_install_breakpoints({directory!r})\n"
    )

    def pyrust_exec(
        interpreter: object,
        code: object,
        shared: object = None,
        *,
        restrict: bool = False,
    ) -> object:
        if isinstance(code, str):
            code = bootstrap + code
        if shared is None:
            return original_exec(interpreter, code, restrict=restrict)
        return original_exec(interpreter, code, shared, restrict=restrict)

    _interpreters.exec = pyrust_exec
    _INTERPRETERS_EXEC_HOOK_INSTALLED = True


class _BreakpointTracer:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._config = root / "breakpoints.json"
        self._config_mtime_ns = -1
        self._next_refresh = 0.0
        self._breakpoints: dict[str, frozenset[int]] = {}
        self._module_path = str(Path(__file__).resolve())

    def __call__(
        self,
        frame: FrameType,
        event: str,
        arg: object,
    ) -> object:
        del arg
        if event != "line" or frame.f_code.co_filename == self._module_path:
            return self
        self._refresh()
        lines = self._breakpoints.get(frame.f_code.co_filename)
        if lines is None or frame.f_lineno not in lines:
            return self
        self._stop(frame)
        return frame.f_trace

    def _refresh(self) -> None:
        now = time.monotonic()
        if now < self._next_refresh:
            return
        self._next_refresh = now + 0.025
        try:
            modified = self._config.stat().st_mtime_ns
        except OSError:
            modified = -1
        if modified == self._config_mtime_ns:
            return
        self._config_mtime_ns = modified
        payload = _read_json(self._config)
        raw_records = payload.get("breakpoints") if payload else None
        records: dict[str, frozenset[int]] = {}
        if isinstance(raw_records, list):
            for record in raw_records:
                if not isinstance(record, dict):
                    continue
                path = record.get("path")
                raw_lines = record.get("lines")
                if not isinstance(path, str) or not isinstance(raw_lines, list):
                    continue
                lines = frozenset(
                    line
                    for line in raw_lines
                    if isinstance(line, int)
                    and not isinstance(line, bool)
                    and line > 0
                )
                if lines:
                    records[path] = lines
        self._breakpoints = records

    def _stop(self, frame: FrameType) -> None:
        process_id = os.getpid()
        native_thread_id = threading.get_native_id()
        active = self._root / f"active-{process_id}"
        while True:
            try:
                active.mkdir()
                break
            except FileExistsError:
                time.sleep(0.002)
        nonce = time.time_ns()
        stop_root = self._root / f"stop-{process_id}-{native_thread_id}-{nonce}"
        stop_root.mkdir()
        marker = self._root / f"stopped-{process_id}-{native_thread_id}-{nonce}.json"
        sys.settrace(None)
        frame.f_trace = None

        def final_continue() -> None:
            active.rmdir()
            sys.settrace(self)

        try:
            _write_json(
                marker,
                {
                    "pid": process_id,
                    "threadId": native_thread_id,
                    "root": str(stop_root),
                    "path": frame.f_code.co_filename,
                    "line": frame.f_lineno,
                    "name": frame.f_code.co_name,
                },
            )
            action = _LiveFrameService(
                stop_root,
                frame,
                generation=1,
                on_final_continue=final_continue,
            ).run()
        except BaseException:
            try:
                active.rmdir()
            except OSError:
                pass
            raise
        finally:
            marker.unlink(missing_ok=True)
        if action == "continue":
            sys.settrace(self)
            frame.f_trace = self


class _LiveFrameService:
    def __init__(
        self,
        root: Path,
        selected: FrameType,
        *,
        generation: int,
        on_final_continue: Callable[[], None] | None = None,
    ) -> None:
        self._root = root
        self._selected = selected
        self._generation = generation
        self._on_final_continue = on_final_continue
        self._frames: dict[int, FrameType] = {}
        self._objects: dict[int, tuple[object, FrameType]] = {}
        self._next_object = 1
        frame = selected
        frame_id = 1
        while frame is not None:
            self._frames[frame_id] = frame
            frame_id += 1
            frame = frame.f_back

    def run(self) -> str:
        _write_json(
            self._root / "ready.json",
            {
                "generation": self._generation,
                "frames": [
                    {
                        "id": frame_id,
                        "name": frame.f_code.co_name,
                        "path": frame.f_code.co_filename,
                        "line": frame.f_lineno,
                    }
                    for frame_id, frame in self._frames.items()
                ]
            },
        )
        request_path = self._root / "request.json"
        existing = _read_json(request_path)
        raw_sequence = existing.get("seq") if existing is not None else None
        sequence = raw_sequence if isinstance(raw_sequence, int) else 0
        while True:
            request = _read_json(request_path)
            if request is None or request.get("seq") == sequence:
                time.sleep(0.002)
                continue
            raw_sequence = request.get("seq")
            if not isinstance(raw_sequence, int) or raw_sequence <= sequence:
                time.sleep(0.002)
                continue
            sequence = raw_sequence
            command = request.get("command")
            try:
                body, release = self._dispatch(
                    str(command),
                    request.get("arguments"),
                )
                response = {"seq": sequence, "success": True, "body": body}
            except Exception as error:
                release = False
                response = {
                    "seq": sequence,
                    "success": False,
                    "message": f"{type(error).__name__}: {error}",
                }
            _write_json(self._root / f"response-{sequence}.json", response)
            if release:
                if command == "continue" and self._on_final_continue is not None:
                    self._on_final_continue()
                return command

    def _dispatch(
        self,
        command: str,
        raw_arguments: object,
    ) -> tuple[dict[str, Any], bool]:
        arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
        if command == "scopes":
            return self._scopes(arguments), False
        if command == "variables":
            return self._variables(arguments), False
        if command == "evaluate":
            return self._evaluate(arguments), False
        if command == "setVariable":
            return self._set_variable(arguments), False
        if command == "setExpression":
            return self._set_expression(arguments), False
        if command == "continue":
            return {}, True
        if command in {"next", "stepIn", "stepOut"}:
            self._prepare_step(command, self._frame(arguments))
            return {}, True
        raise LiveLeaseError(f"unsupported live lease command {command!r}")

    def _prepare_step(self, command: str, selected: FrameType) -> None:
        import sys

        target = selected.f_back if command == "stepOut" else selected
        if target is None:
            raise LiveLeaseError(f"Python {command} has no target frame")
        fired = False

        def tracer(frame: FrameType, event: str, arg: object) -> object:
            nonlocal fired
            del arg
            matches = (
                frame is target and event in {"line", "return", "exception"}
                if command != "stepIn"
                else frame.f_back is selected and event == "call"
            )
            if not fired and matches:
                fired = True
                sys.settrace(None)
                frame.f_trace = None
                _LiveFrameService(
                    self._root,
                    frame,
                    generation=self._generation + 1,
                    on_final_continue=self._on_final_continue,
                ).run()
                return None
            return tracer

        target.f_trace = tracer
        selected.f_trace = tracer
        sys.settrace(tracer)

    def _scopes(self, arguments: Mapping[str, object]) -> dict[str, Any]:
        frame = self._frame(arguments)
        return {
            "scopes": [
                {
                    "name": "Python Locals",
                    "presentationHint": "locals",
                    "variablesReference": self._register(frame.f_locals, frame),
                    "expensive": False,
                },
                {
                    "name": "Python Globals",
                    "presentationHint": "globals",
                    "variablesReference": self._register(frame.f_globals, frame),
                    "expensive": True,
                },
            ]
        }

    def _variables(self, arguments: Mapping[str, object]) -> dict[str, Any]:
        reference = _positive_int(arguments.get("variablesReference"), "reference")
        try:
            value, frame = self._objects[reference]
        except KeyError as error:
            raise LiveLeaseError(f"unknown variables reference {reference}") from error
        if isinstance(value, Mapping):
            items = sorted(
                ((str(name), item) for name, item in value.items()),
                key=lambda pair: pair[0],
            )
        elif isinstance(value, (list, tuple)):
            items = [(str(index), item) for index, item in enumerate(value)]
        elif isinstance(value, (set, frozenset)):
            items = [
                (str(index), item)
                for index, item in enumerate(sorted(value, key=repr))
            ]
        else:
            try:
                items = sorted(vars(value).items())
            except (TypeError, AttributeError):
                items = []
        return {
            "variables": [
                self._variable(name, item, frame)
                for name, item in items
            ]
        }

    def _evaluate(self, arguments: Mapping[str, object]) -> dict[str, Any]:
        frame = self._frame(arguments)
        expression = arguments.get("expression")
        if not isinstance(expression, str):
            raise LiveLeaseError("Python evaluation requires a string expression")
        try:
            value = eval(expression, frame.f_globals, frame.f_locals)
        except SyntaxError:
            if arguments.get("context") != "repl":
                raise
            exec(expression, frame.f_globals, frame.f_locals)
            value = None
        return self._value_body(value, frame)

    def _set_variable(self, arguments: Mapping[str, object]) -> dict[str, Any]:
        reference = _positive_int(arguments.get("variablesReference"), "reference")
        name = arguments.get("name")
        expression = arguments.get("value")
        if not isinstance(name, str) or not isinstance(expression, str):
            raise LiveLeaseError("setVariable requires string name and value")
        try:
            target, frame = self._objects[reference]
        except KeyError as error:
            raise LiveLeaseError(f"unknown variables reference {reference}") from error
        if not hasattr(target, "__setitem__"):
            raise LiveLeaseError("selected variable container is not writable")
        value = eval(expression, frame.f_globals, frame.f_locals)
        target[name] = value  # type: ignore[index]
        return self._value_body(value, frame)

    def _set_expression(self, arguments: Mapping[str, object]) -> dict[str, Any]:
        frame = self._frame(arguments)
        expression = arguments.get("expression")
        value_expression = arguments.get("value")
        if not isinstance(expression, str) or not isinstance(value_expression, str):
            raise LiveLeaseError("setExpression requires string expressions")
        value = eval(value_expression, frame.f_globals, frame.f_locals)
        namespace = dict(frame.f_locals)
        namespace["_pyrust_assignment_value"] = value
        exec(
            f"{expression} = _pyrust_assignment_value",
            frame.f_globals,
            namespace,
        )
        if expression.isidentifier():
            frame.f_locals[expression] = namespace[expression]
        return self._value_body(value, frame)

    def _frame(self, arguments: Mapping[str, object]) -> FrameType:
        frame_id = _positive_int(arguments.get("frameId"), "frame ID")
        try:
            return self._frames[frame_id]
        except KeyError as error:
            raise LiveLeaseError(f"unknown frame ID {frame_id}") from error

    def _register(self, value: object, frame: FrameType) -> int:
        reference = self._next_object
        self._next_object += 1
        self._objects[reference] = (value, frame)
        return reference

    def _variable(self, name: str, value: object, frame: FrameType) -> dict[str, Any]:
        body = self._value_body(value, frame)
        body["name"] = name
        return body

    def _value_body(self, value: object, frame: FrameType) -> dict[str, Any]:
        expandable = isinstance(
            value,
            (Mapping, list, tuple, set, frozenset),
        ) or hasattr(value, "__dict__")
        return {
            "result": repr(value),
            "value": repr(value),
            "type": type(value).__name__,
            "variablesReference": self._register(value, frame) if expandable else 0,
        }


def _find_frame(expected_name: str, expected_path: str) -> FrameType:
    frame = _current_parent_frame()
    matches: list[FrameType] = []
    while frame is not None:
        if (
            frame.f_code.co_name == expected_name
            and frame.f_code.co_filename == expected_path
        ):
            matches.append(frame)
        frame = frame.f_back
    if len(matches) != 1:
        raise LiveLeaseError(
            "selected Python frame was not uniquely present in the live stack"
        )
    return matches[0]


def _current_parent_frame() -> FrameType | None:
    import sys

    frame = sys._getframe()
    return frame.f_back.f_back if frame.f_back is not None else None


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise LiveLeaseError(f"{label} must be a positive integer")
    return value


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
