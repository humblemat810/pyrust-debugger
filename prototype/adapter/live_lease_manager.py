"""DAP-facing client for the interpreter-local live Python frame service."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from threading import Lock
import time
from typing import Any, Callable, Mapping

from prototype.python.pyrust_stack.remote_debug import (
    queue_remote_debug_script,
    RemoteDebugError,
    selected_python_interpreter_id,
)


class LiveLeaseTransportError(RuntimeError):
    """The interpreter-local live frame service failed."""


@dataclass
class _Lease:
    process_id: int
    native_thread_id: int
    root: Path
    frames: tuple[dict[str, Any], ...]
    generation: int
    sequence: int = 0
    request_lock: Lock = field(default_factory=Lock)


@dataclass(frozen=True)
class _VariableRoute:
    lease: _Lease
    raw_reference: int


class LiveLeaseManager:
    """Route live operations to a secondary interpreter without debugpy."""

    def __init__(self, root: Path) -> None:
        self._root = root / "subinterpreter-live"
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._leases: dict[int, _Lease] = {}
        self._frame_routes: dict[int, tuple[_Lease, int]] = {}
        self._frame_keys: dict[tuple[int, int], int] = {}
        self._variable_routes: dict[int, _VariableRoute] = {}
        self._next_frame = 1_925_000_000
        self._next_reference = 1_950_000_000
        self._generation = 0

    def owns_frame(self, frame_id: object) -> bool:
        if not isinstance(frame_id, int):
            return False
        with self._lock:
            return frame_id in self._frame_routes

    def owns_variable(self, reference: object) -> bool:
        if not isinstance(reference, int):
            return False
        with self._lock:
            return reference in self._variable_routes

    def owns_thread(self, thread_id: object) -> bool:
        if not isinstance(thread_id, int):
            return False
        with self._lock:
            return any(
                lease.native_thread_id == thread_id
                for lease in self._leases.values()
            )

    def has_lease(self) -> bool:
        with self._lock:
            return bool(self._leases)

    def threads(self) -> list[dict[str, Any]]:
        with self._lock:
            leases = tuple(self._leases.values())
        return [
            {
                "id": lease.native_thread_id,
                "name": (
                    f"process {lease.process_id}: "
                    "secondary-interpreter Python thread"
                ),
            }
            for lease in leases
        ]

    def stack_trace(self, thread_id: int) -> dict[str, Any]:
        with self._lock:
            leases = tuple(self._leases.values())
        lease = next(
            (
                candidate
                for candidate in leases
                if candidate.native_thread_id == thread_id
            ),
            None,
        )
        if lease is None:
            raise LiveLeaseTransportError(
                f"unknown live Python thread {thread_id}"
            )
        frames = [
            {
                "id": self._allocate_frame(lease, int(frame["id"])),
                "name": frame.get("name", "<python>"),
                "source": {
                    "name": Path(str(frame.get("path", ""))).name,
                    "path": frame.get("path", ""),
                },
                "line": frame.get("line", 1),
                "column": 1,
                "presentationHint": "normal",
            }
            for frame in lease.frames
            if isinstance(frame.get("id"), int)
        ]
        return {"stackFrames": frames, "totalFrames": len(frames)}

    def process_for_thread(self, thread_id: int) -> int | None:
        with self._lock:
            leases = tuple(self._leases.values())
        return next(
            (
                lease.process_id
                for lease in leases
                if lease.native_thread_id == thread_id
            ),
            None,
        )

    def is_secondary_frame(
        self,
        process_id: int,
        native_thread_id: int,
        *,
        name: str,
        path: str,
    ) -> bool:
        try:
            return selected_python_interpreter_id(
                process_id,
                native_thread_id,
                expected_name=name,
                expected_path=path,
            ) != 0
        except RemoteDebugError as error:
            raise LiveLeaseTransportError(str(error)) from error

    def arm(
        self,
        process_id: int,
        native_thread_id: int,
        *,
        frame_id: int,
        name: str,
        path: str,
        resume_native: Callable[[], None],
    ) -> None:
        with self._lock:
            self._generation += 1
            root = self._root / f"{process_id}-{native_thread_id}-{self._generation}"
        root.mkdir(parents=True, exist_ok=False)
        script = root / "enter.py"
        script.write_text(
            "from pyrust_stack.live_lease import run_live_lease\n"
            f"run_live_lease({str(root)!r}, {name!r}, {path!r})\n",
            encoding="utf-8",
        )
        try:
            queue_remote_debug_script(
                process_id,
                native_thread_id,
                script,
                expected_name=name,
                expected_path=path,
            )
        except RemoteDebugError as error:
            raise LiveLeaseTransportError(str(error)) from error
        resume_native()
        lease = self._wait_ready(process_id, native_thread_id, root)
        self._bind_frame(lease, frame_id, name, path)
        for frame in lease.frames:
            raw_frame = frame.get("id")
            if isinstance(raw_frame, int):
                self._allocate_frame(lease, raw_frame)
        with self._lock:
            self._leases[process_id] = lease

    def bind_frame(
        self,
        frame_id: int,
        *,
        process_id: int,
        name: str,
        path: str,
    ) -> bool:
        with self._lock:
            lease = self._leases.get(process_id)
        if lease is None:
            return False
        self._bind_frame(lease, frame_id, name, path)
        return True

    def scopes(self, frame_id: int) -> dict[str, Any]:
        lease, raw_frame = self._require_frame(frame_id)
        return self._request(
            lease,
            "scopes",
            {"frameId": raw_frame},
            translate=True,
        )

    def variables(self, reference: int) -> dict[str, Any]:
        with self._lock:
            route = self._variable_routes.get(reference)
        if route is None:
            raise LiveLeaseTransportError(
                f"unknown live Python variables reference {reference}"
            )
        return self._request(
            route.lease,
            "variables",
            {"variablesReference": route.raw_reference},
            translate=True,
        )

    def evaluate(
        self,
        frame_id: int,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        lease, raw_frame = self._require_frame(frame_id)
        return self._request(
            lease,
            "evaluate",
            {
                "frameId": raw_frame,
                "expression": arguments.get("expression", ""),
                "context": arguments.get("context", "watch"),
            },
            translate=True,
        )

    def set_variable(
        self,
        reference: int,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            route = self._variable_routes.get(reference)
        if route is None:
            raise LiveLeaseTransportError(
                f"unknown live Python variables reference {reference}"
            )
        return self._request(
            route.lease,
            "setVariable",
            {
                "variablesReference": route.raw_reference,
                "name": arguments.get("name", ""),
                "value": arguments.get("value", ""),
            },
            translate=True,
        )

    def set_expression(
        self,
        frame_id: int,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        lease, raw_frame = self._require_frame(frame_id)
        return self._request(
            lease,
            "setExpression",
            {
                "frameId": raw_frame,
                "expression": arguments.get("expression", ""),
                "value": arguments.get("value", ""),
            },
            translate=True,
        )

    def begin_step(
        self,
        frame_id: int,
        command: str,
    ) -> tuple[int, int, int]:
        if command not in {"next", "stepIn", "stepOut"}:
            raise LiveLeaseTransportError(
                f"unsupported live Python step {command!r}"
            )
        lease, raw_frame = self._require_frame(frame_id)
        generation = lease.generation
        self._request(
            lease,
            command,
            {"frameId": raw_frame},
            translate=False,
        )
        return lease.process_id, lease.native_thread_id, generation

    def finish_step(
        self,
        process_id: int,
        generation: int,
    ) -> int:
        with self._lock:
            lease = self._leases.get(process_id)
        if lease is None:
            raise LiveLeaseTransportError(
                f"live Python process {process_id} has no active lease"
            )
        ready = lease.root / "ready.json"
        deadline = time.monotonic() + 10
        frames: tuple[dict[str, Any], ...] | None = None
        next_generation: int | None = None
        while time.monotonic() < deadline:
            payload = _read_json(ready)
            raw_generation = payload.get("generation") if payload else None
            raw_frames = payload.get("frames") if payload else None
            if (
                isinstance(raw_generation, int)
                and raw_generation > generation
                and isinstance(raw_frames, list)
            ):
                next_generation = raw_generation
                frames = tuple(
                    frame for frame in raw_frames if isinstance(frame, dict)
                )
                break
            time.sleep(0.002)
        if frames is None or next_generation is None:
            raise LiveLeaseTransportError("live Python step did not reach another frame")
        with self._lock:
            lease.frames = frames
            lease.generation = next_generation
            self._frame_routes = {
                frame_id: route
                for frame_id, route in self._frame_routes.items()
                if route[0] is not lease
            }
            self._frame_keys = {
                key: frame_id
                for key, frame_id in self._frame_keys.items()
                if key[0] != id(lease)
            }
            self._variable_routes = {
                reference: route
                for reference, route in self._variable_routes.items()
                if route.lease is not lease
            }
        for frame in frames:
            raw_frame = frame.get("id")
            if isinstance(raw_frame, int):
                self._allocate_frame(lease, raw_frame)
        return lease.native_thread_id

    def release(self, process_id: int) -> None:
        with self._lock:
            lease = self._leases.get(process_id)
        if lease is None:
            return
        self._request(lease, "continue", {}, translate=False)
        with self._lock:
            if self._leases.get(process_id) is lease:
                self._leases.pop(process_id)
            self._frame_routes = {
                frame_id: route
                for frame_id, route in self._frame_routes.items()
                if route[0] is not lease
            }
            self._frame_keys = {
                key: frame_id
                for key, frame_id in self._frame_keys.items()
                if key[0] != id(lease)
            }
            self._variable_routes = {
                reference: route
                for reference, route in self._variable_routes.items()
                if route.lease is not lease
            }

    def close(self) -> None:
        with self._lock:
            process_ids = tuple(self._leases)
        for process_id in process_ids:
            try:
                self.release(process_id)
            except LiveLeaseTransportError:
                pass

    def _wait_ready(
        self,
        process_id: int,
        native_thread_id: int,
        root: Path,
    ) -> _Lease:
        ready = root / "ready.json"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            payload = _read_json(ready)
            frames = payload.get("frames") if payload is not None else None
            if isinstance(frames, list):
                return _Lease(
                    process_id,
                    native_thread_id,
                    root,
                    tuple(frame for frame in frames if isinstance(frame, dict)),
                    int(payload.get("generation", 1)),
                )
            time.sleep(0.005)
        raise LiveLeaseTransportError("timed out entering live subinterpreter frame")

    def _bind_frame(
        self,
        lease: _Lease,
        frame_id: int,
        name: str,
        path: str,
    ) -> None:
        matches = [
            frame
            for frame in lease.frames
            if frame.get("name") == name and frame.get("path") == path
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("id"), int):
            raise LiveLeaseTransportError(
                f"live Python frame {name!r} at {path!r} was not uniquely found"
            )
        with self._lock:
            self._frame_routes[frame_id] = (lease, matches[0]["id"])

    def _require_frame(self, frame_id: int) -> tuple[_Lease, int]:
        with self._lock:
            route = self._frame_routes.get(frame_id)
        if route is None:
            raise LiveLeaseTransportError(
                f"unknown live Python frame {frame_id}"
            )
        return route

    def _allocate_frame(self, lease: _Lease, raw_frame: int) -> int:
        key = (id(lease), raw_frame)
        with self._lock:
            existing = self._frame_keys.get(key)
            if existing is not None:
                return existing
            frame_id = self._next_frame
            self._next_frame += 1
            self._frame_keys[key] = frame_id
            self._frame_routes[frame_id] = (lease, raw_frame)
            return frame_id

    def _request(
        self,
        lease: _Lease,
        command: str,
        arguments: Mapping[str, Any],
        *,
        translate: bool,
    ) -> dict[str, Any]:
        with lease.request_lock:
            with self._lock:
                lease.sequence += 1
                sequence = lease.sequence
                request_path = lease.root / "request.json"
                temporary = lease.root / ".request.tmp"
                temporary.write_text(
                    json.dumps(
                        {
                            "seq": sequence,
                            "command": command,
                            "arguments": dict(arguments),
                        }
                    ),
                    encoding="utf-8",
                )
                temporary.replace(request_path)
            response_path = lease.root / f"response-{sequence}.json"
            deadline = time.monotonic() + 10
            response: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                response = _read_json(response_path)
                if response is not None:
                    break
                time.sleep(0.002)
        if response is None:
            raise LiveLeaseTransportError(
                f"live Python {command} request timed out"
            )
        if response.get("success") is not True:
            raise LiveLeaseTransportError(
                str(response.get("message", f"live Python {command} failed"))
            )
        body = response.get("body")
        result = dict(body) if isinstance(body, dict) else {}
        return self._translate_body(lease, result) if translate else result

    def _translate_body(
        self,
        lease: _Lease,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        scopes = body.get("scopes")
        if isinstance(scopes, list):
            body["scopes"] = [
                self._translate_value(lease, item)
                for item in scopes
                if isinstance(item, dict)
            ]
        variables = body.get("variables")
        if isinstance(variables, list):
            body["variables"] = [
                self._translate_value(lease, item)
                for item in variables
                if isinstance(item, dict)
            ]
        return self._translate_value(lease, body)

    def _translate_value(
        self,
        lease: _Lease,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        translated = dict(value)
        raw = translated.get("variablesReference")
        if isinstance(raw, int) and raw > 0:
            with self._lock:
                reference = self._next_reference
                self._next_reference += 1
                self._variable_routes[reference] = _VariableRoute(lease, raw)
            translated["variablesReference"] = reference
        return translated


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
