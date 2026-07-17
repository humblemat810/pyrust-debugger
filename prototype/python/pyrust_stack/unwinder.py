"""Stable CPython 3.14 remote-stack helper primitives."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import errno
import os
import sys
from typing import Any

try:
    from _remote_debugging import RemoteUnwinder
except ImportError:  # pragma: no cover - exercised on unsupported runtimes.
    RemoteUnwinder = None


ERROR_MESSAGES = {
    "invalid_pid": "pid must be a positive integer",
    "unsupported_runtime": "pyrust_stack requires CPython 3.14",
    "permission_denied": "permission denied while reading target process",
    "target_exited": "target process exited before stack collection completed",
    "malformed_data": "CPython remote unwinder returned malformed stack data",
    "unwinder_failure": "CPython remote unwinder failed to read target process",
}


class StackReadError(RuntimeError):
    """A helper failure with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        super().__init__(ERROR_MESSAGES[code])
        self.code = code

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


@dataclass(frozen=True)
class Frame:
    name: str
    path: str
    line: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ThreadStack:
    thread_id: int
    frames: tuple[Frame, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "threadId": self.thread_id,
            "frames": [frame.to_dict() for frame in self.frames],
        }


def read_python_stacks(pid: int) -> tuple[ThreadStack, ...]:
    """Read CPython stacks for one process without executing target code.

    Returned thread IDs are OS thread IDs. Frame order is the CPython
    RemoteUnwinder order: newest frame first.
    """
    _validate_pid(pid)
    _require_supported_runtime()
    _check_target_access(pid)

    remote_unwinder = RemoteUnwinder
    if remote_unwinder is None:
        raise StackReadError("unsupported_runtime")
    try:
        raw_threads = remote_unwinder(pid, all_threads=True).get_stack_trace()
    except Exception as error:
        raise _classify_unwinder_error(pid, error) from error

    try:
        return tuple(_normalize_thread(thread) for thread in raw_threads)
    except Exception as error:
        raise StackReadError("malformed_data") from error


def success_payload(stacks: tuple[ThreadStack, ...]) -> dict[str, Any]:
    """Build the successful JSON payload consumed by the DAP proxy."""
    return {"ok": True, "threads": [stack.to_dict() for stack in stacks]}


def failure_payload(error: StackReadError) -> dict[str, Any]:
    """Build a deterministic error payload for every handled helper failure."""
    return {"ok": False, "error": error.to_dict()}


def _validate_pid(pid: int) -> None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise StackReadError("invalid_pid")


def _require_supported_runtime() -> None:
    if sys.version_info[:2] != (3, 14) or RemoteUnwinder is None:
        raise StackReadError("unsupported_runtime")


def _check_target_access(pid: int) -> None:
    try:
        os.kill(pid, 0)
    except PermissionError as error:
        raise StackReadError("permission_denied") from error
    except ProcessLookupError as error:
        raise StackReadError("target_exited") from error
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EPERM}:
            raise StackReadError("permission_denied") from error
        if error.errno == errno.ESRCH:
            raise StackReadError("target_exited") from error
        raise StackReadError("unwinder_failure") from error


def _classify_unwinder_error(pid: int, error: Exception) -> StackReadError:
    if isinstance(error, PermissionError):
        return StackReadError("permission_denied")
    if isinstance(error, ProcessLookupError):
        return StackReadError("target_exited")
    if isinstance(error, OSError):
        if error.errno in {errno.EACCES, errno.EPERM}:
            return StackReadError("permission_denied")
        if error.errno == errno.ESRCH:
            return StackReadError("target_exited")

    try:
        _check_target_access(pid)
    except StackReadError as target_error:
        if target_error.code in {"permission_denied", "target_exited"}:
            return target_error
    return StackReadError("unwinder_failure")


def _normalize_thread(thread: object) -> ThreadStack:
    thread_id = _positive_int(thread.thread_id)
    frames = tuple(_normalize_frame(frame) for frame in thread.frame_info)
    return ThreadStack(thread_id=thread_id, frames=frames)


def _normalize_frame(frame: object) -> Frame:
    name = _non_empty_string(frame.funcname)
    path = _non_empty_string(frame.filename)
    line = _positive_int(frame.lineno)
    return Frame(name=name, path=path, line=line)


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("expected a positive integer")
    return value


def _non_empty_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("expected a non-empty string")
    return value
