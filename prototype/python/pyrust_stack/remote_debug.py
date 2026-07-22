"""Queue a CPython 3.14 remote-debug script on one exact native thread."""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import struct
import sys

from .locals import (
    LocalReadError,
    _find_thread_state,
    _IOVec,
    _read_debug_offsets,
    _RemoteMemory,
    _runtime_address,
)


class RemoteDebugError(RuntimeError):
    """The documented CPython remote-debug request could not be queued."""


class _RemoteWriter:
    def __init__(self, pid: int) -> None:
        self._pid = pid
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            function = libc.process_vm_writev
        except AttributeError as error:  # pragma: no cover - Linux contract.
            raise RemoteDebugError("process_vm_writev is unavailable") from error
        function.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(_IOVec),
            ctypes.c_ulong,
            ctypes.POINTER(_IOVec),
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        function.restype = ctypes.c_ssize_t
        self._writev = function

    def write(self, address: int, data: bytes) -> None:
        if address <= 0 or not data:
            raise RemoteDebugError("remote write request is invalid")
        buffer = ctypes.create_string_buffer(data, len(data))
        local = _IOVec(ctypes.cast(buffer, ctypes.c_void_p), len(data))
        remote = _IOVec(ctypes.c_void_p(address), len(data))
        count = self._writev(
            self._pid,
            ctypes.byref(local),
            1,
            ctypes.byref(remote),
            1,
            0,
        )
        if count == len(data):
            return
        error = ctypes.get_errno()
        if error in {errno.EACCES, errno.EPERM}:
            raise RemoteDebugError(
                "permission denied while queuing remote Python execution"
            )
        if error == errno.ESRCH:
            raise RemoteDebugError(
                "target exited while queuing remote Python execution"
            )
        detail = os.strerror(error) if error else f"wrote {count} of {len(data)} bytes"
        raise RemoteDebugError(f"could not write target memory: {detail}")


def queue_remote_debug_script(
    pid: int,
    native_thread_id: int,
    script: Path,
    *,
    expected_name: str | None = None,
    expected_path: str | None = None,
    require_main_interpreter: bool = False,
) -> None:
    """Schedule ``script`` on the selected CPython thread at its next safe point."""

    if sys.platform != "linux" or sys.maxsize <= 2**32:
        raise RemoteDebugError("targeted remote debugging requires 64-bit Linux")
    if sys.version_info[:2] != (3, 14):
        raise RemoteDebugError("targeted remote debugging requires CPython 3.14")
    if pid <= 0 or native_thread_id <= 0:
        raise RemoteDebugError("pid and native thread ID must be positive")

    path = str(script.resolve()).encode("utf-8")
    try:
        memory = _RemoteMemory(pid)
        offsets = _read_debug_offsets(memory, pid)
        runtime = _runtime_address(pid)
        writer = _RemoteWriter(pid)
        interpreter, thread_state = _find_thread_state(
            memory,
            offsets,
            runtime,
            native_thread_id,
            expected_name=expected_name,
            expected_path=expected_path,
        )
        if (
            require_main_interpreter
            and memory.signed_size(interpreter + offsets.interpreter_id) != 0
        ):
            raise RemoteDebugError(
                "debugpy handoff is unavailable for a CPython subinterpreter"
            )
        enabled_address = (
            interpreter + offsets.interpreter_remote_debugging_enabled
        )
        enabled = struct.unpack(
            "<I",
            memory.read(enabled_address, 4),
        )[0]
        if enabled == 0:
            # PyRust only calls this path for a launch that explicitly enabled
            # the private debugpy coordinator.
            writer.write(enabled_address, struct.pack("<I", 1))
    except LocalReadError as error:
        raise RemoteDebugError(str(error)) from error

    path_size = offsets.debugger_script_path_size
    if path_size <= 1 or len(path) >= path_size:
        raise RemoteDebugError(
            f"remote script path exceeds CPython's {path_size}-byte buffer"
        )

    support = thread_state + offsets.thread_remote_debugger_support
    writer.write(
        support + offsets.debugger_script_path,
        path + b"\0" * (path_size - len(path)),
    )
    writer.write(
        support + offsets.debugger_pending_call,
        struct.pack("<I", 1),
    )
    breaker_address = thread_state + offsets.thread_eval_breaker
    breaker = struct.unpack("<Q", memory.read(breaker_address, 8))[0]
    writer.write(breaker_address, struct.pack("<Q", breaker | (1 << 5)))


def selected_python_interpreter_id(
    pid: int,
    native_thread_id: int,
    *,
    expected_name: str,
    expected_path: str,
) -> int:
    """Return the CPython interpreter ID that owns one selected live frame."""

    if sys.platform != "linux" or sys.maxsize <= 2**32:
        raise RemoteDebugError("targeted remote debugging requires 64-bit Linux")
    if sys.version_info[:2] != (3, 14):
        raise RemoteDebugError("targeted remote debugging requires CPython 3.14")
    try:
        memory = _RemoteMemory(pid)
        offsets = _read_debug_offsets(memory, pid)
        interpreter, _thread_state = _find_thread_state(
            memory,
            offsets,
            _runtime_address(pid),
            native_thread_id,
            expected_name=expected_name,
            expected_path=expected_path,
        )
        return memory.signed_size(interpreter + offsets.interpreter_id)
    except LocalReadError as error:
        raise RemoteDebugError(str(error)) from error
