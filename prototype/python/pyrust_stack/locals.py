"""Read bounded primitive locals from CPython 3.14 process memory.

This reader intentionally supports only the fixed Linux x86_64 CPython 3.14
environment used by the prototype. It uses CPython's exported ``.PyRuntime``
debug-offset table instead of hard-coded structure offsets, and never writes
to or executes code in the target process.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import errno
from pathlib import Path
import os
import struct
import sys
from typing import Any, Final


_MAX_FRAME_DEPTH: Final = 128
_MAX_LOCALS: Final = 256
_MAX_STRING_BYTES: Final = 16 * 1024
_MAX_INTEGER_DIGITS: Final = 64
_PY_TAG_REFCNT: Final = 1
_PY_INT_TAG: Final = 3
_CO_FAST_HIDDEN: Final = 0x10
_PYLONG_SHIFT: Final = 30


class LocalReadError(RuntimeError):
    """Raised when a target's Python locals cannot be read safely."""


@dataclass(frozen=True)
class LocalFrame:
    """One active Python frame with a bounded snapshot of primitive locals."""

    name: str
    path: str
    locals: dict[str, object]


@dataclass(frozen=True)
class _DebugOffsets:
    runtime_interpreters_head: int
    interpreter_threads_head: int
    thread_size: int
    thread_next: int
    thread_current_frame: int
    thread_native_thread_id: int
    frame_size: int
    frame_previous: int
    frame_executable: int
    frame_localsplus: int
    frame_owner: int
    code_size: int
    code_filename: int
    code_qualname: int
    code_localsplusnames: int
    code_localspluskinds: int
    tuple_ob_item: int
    tuple_ob_size: int
    bytes_ob_size: int
    bytes_ob_sval: int
    pyobject_ob_type: int
    type_tp_name: int
    float_ob_fval: int
    long_lv_tag: int
    long_ob_digit: int
    unicode_state: int
    unicode_length: int
    unicode_asciiobject_size: int


class _IOVec(ctypes.Structure):
    _fields_ = [
        ("iov_base", ctypes.c_void_p),
        ("iov_len", ctypes.c_size_t),
    ]


class _RemoteMemory:
    """Bounded Linux ``process_vm_readv`` access to a traced process."""

    def __init__(self, pid: int) -> None:
        self._pid = pid
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            function = libc.process_vm_readv
        except AttributeError as error:  # pragma: no cover - Linux-only contract.
            raise LocalReadError("process_vm_readv is unavailable") from error
        function.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(_IOVec),
            ctypes.c_ulong,
            ctypes.POINTER(_IOVec),
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        function.restype = ctypes.c_ssize_t
        self._readv = function

    def read(self, address: int, size: int) -> bytes:
        if address <= 0 or size <= 0 or size > _MAX_STRING_BYTES:
            raise LocalReadError("remote read request is outside supported bounds")
        buffer = ctypes.create_string_buffer(size)
        local = _IOVec(ctypes.cast(buffer, ctypes.c_void_p), size)
        remote = _IOVec(ctypes.c_void_p(address), size)
        count = self._readv(
            self._pid,
            ctypes.byref(local),
            1,
            ctypes.byref(remote),
            1,
            0,
        )
        if count != size:
            error = ctypes.get_errno()
            if error in {errno.EACCES, errno.EPERM}:
                raise LocalReadError("permission denied while reading Python locals")
            if error == errno.ESRCH:
                raise LocalReadError("target exited while reading Python locals")
            detail = os.strerror(error) if error else f"read {count} of {size} bytes"
            raise LocalReadError(f"could not read target memory: {detail}")
        return buffer.raw

    def pointer(self, address: int) -> int:
        return struct.unpack("<Q", self.read(address, 8))[0]

    def signed_size(self, address: int) -> int:
        return struct.unpack("<q", self.read(address, 8))[0]


def read_python_locals(pid: int, thread_id: int) -> tuple[LocalFrame, ...]:
    """Return newest-first frame-local snapshots for one CPython thread.

    Values are limited to ``None``, booleans, integers, floats, strings, and
    bytes. Other objects remain visible as typed placeholders but cannot be
    evaluated. The target is read-only throughout this operation.
    """

    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or isinstance(thread_id, bool)
        or not isinstance(thread_id, int)
        or thread_id <= 0
    ):
        raise LocalReadError("pid and thread ID must be positive integers")
    if sys.platform != "linux" or sys.maxsize <= 2**32:
        raise LocalReadError("Python locals require 64-bit Linux")
    if sys.version_info[:2] != (3, 14):
        raise LocalReadError("Python locals require CPython 3.14")

    memory = _RemoteMemory(pid)
    offsets = _read_debug_offsets(memory, pid)
    tstate = _find_thread_state(memory, offsets, _runtime_address(pid), thread_id)
    current_frame = memory.pointer(tstate + offsets.thread_current_frame)
    return _read_frames(memory, offsets, current_frame)


def _read_debug_offsets(memory: _RemoteMemory, pid: int) -> _DebugOffsets:
    runtime = _runtime_address(pid)
    # The fixed 3.14 table is an 8-byte cookie plus 94 uint64 fields.
    raw = memory.read(runtime, 8 + 94 * 8)
    if raw[:8] != b"xdebugpy":
        raise LocalReadError("target does not expose CPython debug offsets")
    values = struct.unpack_from("<94Q", raw, 8)
    version = values[0]
    if version >> 16 != sys.hexversion >> 16:
        raise LocalReadError(
            f"target CPython version 0x{version:x} is incompatible with 3.14"
        )
    if values[1]:
        raise LocalReadError("free-threaded CPython locals are not supported")

    position = 2

    def section(*names: str) -> dict[str, int]:
        nonlocal position
        result = dict(zip(names, values[position : position + len(names)], strict=True))
        position += len(names)
        return result

    runtime_offsets = section("size", "finalizing", "interpreters_head")
    interpreter = section(
        "size",
        "id",
        "next",
        "threads_head",
        "threads_main",
        "gc",
        "imports_modules",
        "sysdict",
        "builtins",
        "ceval_gil",
        "gil_runtime_state",
        "gil_runtime_state_enabled",
        "gil_runtime_state_locked",
        "gil_runtime_state_holder",
        "code_object_generation",
        "tlbc_generation",
    )
    thread = section(
        "size",
        "prev",
        "next",
        "interp",
        "current_frame",
        "thread_id",
        "native_thread_id",
        "datastack_chunk",
        "status",
    )
    frame = section(
        "size",
        "previous",
        "executable",
        "instr_ptr",
        "localsplus",
        "owner",
        "stackpointer",
        "tlbc_index",
    )
    code = section(
        "size",
        "filename",
        "name",
        "qualname",
        "linetable",
        "firstlineno",
        "argcount",
        "localsplusnames",
        "localspluskinds",
        "co_code_adaptive",
        "co_tlbc",
    )
    pyobject = section("size", "ob_type")
    type_object = section("size", "tp_name", "tp_repr", "tp_flags")
    tuple_object = section("size", "ob_item", "ob_size")
    section("size", "ob_item", "ob_size")  # list
    section("size", "used", "table", "mask")  # set
    section("size", "ma_keys", "ma_values")  # dict
    float_object = section("size", "ob_fval")
    long_object = section("size", "lv_tag", "ob_digit")
    bytes_object = section("size", "ob_size", "ob_sval")
    unicode_object = section("size", "state", "length", "asciiobject_size")
    section("size", "collecting")  # GC
    section("size", "gi_name", "gi_iframe", "gi_frame_state")  # generator
    section("next", "prev")  # linked list
    section(
        "eval_breaker",
        "remote_debugger_support",
        "remote_debugging_enabled",
        "debugger_pending_call",
        "debugger_script_path",
        "debugger_script_path_size",
    )
    if position != len(values):
        raise LocalReadError("target debug-offset table has an unexpected layout")

    for label, size in (
        ("thread", thread["size"]),
        ("frame", frame["size"]),
        ("code", code["size"]),
    ):
        if size <= 0 or size > _MAX_STRING_BYTES:
            raise LocalReadError(f"target {label} layout has an invalid size")

    return _DebugOffsets(
        runtime_interpreters_head=runtime_offsets["interpreters_head"],
        interpreter_threads_head=interpreter["threads_head"],
        thread_size=thread["size"],
        thread_next=thread["next"],
        thread_current_frame=thread["current_frame"],
        thread_native_thread_id=thread["native_thread_id"],
        frame_size=frame["size"],
        frame_previous=frame["previous"],
        frame_executable=frame["executable"],
        frame_localsplus=frame["localsplus"],
        frame_owner=frame["owner"],
        code_size=code["size"],
        code_filename=code["filename"],
        code_qualname=code["qualname"],
        code_localsplusnames=code["localsplusnames"],
        code_localspluskinds=code["localspluskinds"],
        tuple_ob_item=tuple_object["ob_item"],
        tuple_ob_size=tuple_object["ob_size"],
        bytes_ob_size=bytes_object["ob_size"],
        bytes_ob_sval=bytes_object["ob_sval"],
        pyobject_ob_type=pyobject["ob_type"],
        type_tp_name=type_object["tp_name"],
        float_ob_fval=float_object["ob_fval"],
        long_lv_tag=long_object["lv_tag"],
        long_ob_digit=long_object["ob_digit"],
        unicode_state=unicode_object["state"],
        unicode_length=unicode_object["length"],
        unicode_asciiobject_size=unicode_object["asciiobject_size"],
    )


def _runtime_address(pid: int) -> int:
    try:
        executable = Path(os.readlink(f"/proc/{pid}/exe")).resolve()
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EPERM}:
            raise LocalReadError("permission denied while locating target Python") from error
        if error.errno == errno.ESRCH:
            raise LocalReadError("target exited while locating target Python") from error
        raise LocalReadError("could not locate target Python executable") from error
    candidates = [executable, *_mapped_python_binaries(pid)]
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            elf_type, section_address = _elf_section_address(candidate, ".PyRuntime")
        except LocalReadError:
            continue
        if elf_type == 2:  # ET_EXEC uses fixed virtual addresses.
            return section_address
        if elf_type == 3:  # ET_DYN is relocated by the loader.
            return _mapped_base(pid, candidate) + section_address
    raise LocalReadError("could not locate CPython's .PyRuntime section")


def _mapped_base(pid: int, executable: Path) -> int:
    try:
        rows = Path(f"/proc/{pid}/maps").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise LocalReadError("could not read target memory map") from error
    for row in rows:
        fields = row.split(maxsplit=5)
        if len(fields) < 6:
            continue
        range_text, _, offset_text, _, _, mapped_path = fields
        mapped_path = mapped_path.removesuffix(" (deleted)")
        try:
            if not os.path.samefile(mapped_path, executable):
                continue
        except OSError:
            continue
        start = int(range_text.partition("-")[0], 16)
        offset = int(offset_text, 16)
        return start - offset
    raise LocalReadError("could not locate the target Python executable mapping")


def _mapped_python_binaries(pid: int) -> tuple[Path, ...]:
    try:
        rows = Path(f"/proc/{pid}/maps").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise LocalReadError("could not read target memory map") from error
    candidates: list[Path] = []
    for row in rows:
        fields = row.split(maxsplit=5)
        if len(fields) < 6:
            continue
        mapped_path = fields[5].removesuffix(" (deleted)")
        path = Path(mapped_path)
        if "python" not in path.name.lower() or not path.is_file():
            continue
        candidates.append(path.resolve())
    return tuple(dict.fromkeys(candidates))


def _elf_section_address(path: Path, section_name: str) -> tuple[int, int]:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise LocalReadError("could not read target Python executable") from error
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise LocalReadError("target Python executable is not little-endian ELF64")
    try:
        (
            _,
            elf_type,
            _,
            _,
            _,
            _,
            section_offset,
            _,
            _,
            _,
            _,
            section_size,
            section_count,
            names_index,
        ) = (
            struct.unpack_from("<16sHHIQQQIHHHHHH", data, 0)
        )
    except struct.error as error:
        raise LocalReadError("target Python executable has an invalid ELF header") from error
    if section_count == 0 or names_index >= section_count:
        raise LocalReadError("target Python executable has no ELF section names")
    entry_size = section_size
    if entry_size < 64:
        raise LocalReadError("target Python executable has invalid ELF sections")
    names_header = struct.unpack_from(
        "<IIQQQQIIQQ",
        data,
        section_offset + names_index * entry_size,
    )
    names_offset, names_size = names_header[4], names_header[5]
    names = data[names_offset : names_offset + names_size]
    for index in range(section_count):
        entry = struct.unpack_from("<IIQQQQIIQQ", data, section_offset + index * entry_size)
        name_offset, _, _, address, _, _, _, _, _, _ = entry
        if name_offset >= len(names):
            continue
        end = names.find(b"\0", name_offset)
        if end < 0:
            continue
        if names[name_offset:end].decode("ascii", "replace") == section_name:
            return elf_type, address
    raise LocalReadError(f"target Python executable has no {section_name} section")


def _find_thread_state(
    memory: _RemoteMemory,
    offsets: _DebugOffsets,
    runtime: int,
    native_thread_id: int,
) -> int:
    interpreter = memory.pointer(runtime + offsets.runtime_interpreters_head)
    if not interpreter:
        raise LocalReadError("target has no active CPython interpreter")
    thread = memory.pointer(interpreter + offsets.interpreter_threads_head)
    for _ in range(_MAX_FRAME_DEPTH):
        if not thread:
            break
        record = memory.read(thread, offsets.thread_size)
        thread_native_id = _u64(record, offsets.thread_native_thread_id)
        if thread_native_id == native_thread_id:
            return thread
        thread = _u64(record, offsets.thread_next)
    raise LocalReadError(f"target has no CPython state for OS thread {native_thread_id}")


def _read_frames(
    memory: _RemoteMemory,
    offsets: _DebugOffsets,
    frame_address: int,
) -> tuple[LocalFrame, ...]:
    frames: list[LocalFrame] = []
    for _ in range(_MAX_FRAME_DEPTH):
        if not frame_address:
            break
        frame = memory.read(frame_address, offsets.frame_size)
        previous = _u64(frame, offsets.frame_previous)
        executable = _u64(frame, offsets.frame_executable) & ~_PY_TAG_REFCNT
        owner = _u8(frame, offsets.frame_owner)
        if executable and owner in {0, 1, 2}:
            parsed = _read_frame(memory, offsets, frame_address, frame, executable)
            if parsed is not None:
                frames.append(parsed)
        frame_address = previous
    return tuple(frames)


def _read_frame(
    memory: _RemoteMemory,
    offsets: _DebugOffsets,
    frame_address: int,
    frame: bytes,
    code_address: int,
) -> LocalFrame | None:
    code = memory.read(code_address, offsets.code_size)
    name = _read_unicode(memory, offsets, _u64(code, offsets.code_qualname))
    path = _read_unicode(memory, offsets, _u64(code, offsets.code_filename))
    if not name or not path:
        return None
    names = _read_tuple_strings(
        memory,
        offsets,
        _u64(code, offsets.code_localsplusnames),
    )
    kinds = _read_bytes(memory, offsets, _u64(code, offsets.code_localspluskinds))
    if len(names) != len(kinds):
        raise LocalReadError("target code object has inconsistent local metadata")
    locals_address = frame_address + offsets.frame_localsplus
    values: dict[str, object] = {}
    for index, (local_name, kind) in enumerate(zip(names, kinds, strict=True)):
        if index >= _MAX_LOCALS:
            break
        if kind & _CO_FAST_HIDDEN:
            continue
        reference = _u64(memory.read(locals_address + index * 8, 8), 0)
        if reference == _PY_TAG_REFCNT:
            continue
        values[local_name] = _read_value(memory, offsets, reference)
    return LocalFrame(name=name, path=path, locals=values)


def _read_tuple_strings(
    memory: _RemoteMemory,
    offsets: _DebugOffsets,
    tuple_address: int,
) -> tuple[str, ...]:
    if not tuple_address:
        return ()
    size = memory.signed_size(tuple_address + offsets.tuple_ob_size)
    if size < 0 or size > _MAX_LOCALS:
        raise LocalReadError("target tuple of local names exceeds supported bounds")
    # PyTupleObject stores ``ob_item`` inline; the offset points at its first
    # pointer rather than at a separately allocated pointer array.
    items = tuple_address + offsets.tuple_ob_item
    return tuple(
        _read_unicode(memory, offsets, memory.pointer(items + index * 8))
        for index in range(size)
    )


def _read_bytes(
    memory: _RemoteMemory,
    offsets: _DebugOffsets,
    address: int,
) -> bytes:
    if not address:
        return b""
    size = memory.signed_size(address + offsets.bytes_ob_size)
    if size < 0 or size > _MAX_LOCALS:
        raise LocalReadError("target byte value exceeds supported bounds")
    return memory.read(address + offsets.bytes_ob_sval, max(size, 1))[:size]


def _read_unicode(
    memory: _RemoteMemory,
    offsets: _DebugOffsets,
    address: int,
) -> str:
    if not address:
        raise LocalReadError("target string pointer is null")
    state = struct.unpack("<I", memory.read(address + offsets.unicode_state, 4))[0]
    length = memory.signed_size(address + offsets.unicode_length)
    compact = bool(state & (1 << 5))
    ascii_only = bool(state & (1 << 6))
    kind = (state >> 2) & 0x7
    if length < 0 or kind not in {1, 2, 4}:
        raise LocalReadError("target Unicode value has an invalid layout")
    byte_count = length * kind
    if byte_count > _MAX_STRING_BYTES:
        raise LocalReadError("target string exceeds supported bounds")
    if not compact:
        raise LocalReadError("non-compact target strings are not supported")
    data = address + offsets.unicode_asciiobject_size
    if not ascii_only:
        data += 16  # PyCompactUnicodeObject's utf8_length and utf8 pointer.
    raw = memory.read(data, max(byte_count, 1))[:byte_count]
    if ascii_only:
        return raw.decode("ascii")
    return raw.decode({1: "latin-1", 2: "utf-16-le", 4: "utf-32-le"}[kind])


def _read_value(
    memory: _RemoteMemory,
    offsets: _DebugOffsets,
    reference: int,
) -> object:
    if reference & _PY_INT_TAG == _PY_INT_TAG:
        signed = reference if reference < 2**63 else reference - 2**64
        return signed >> 2
    address = reference & ~_PY_TAG_REFCNT
    if not address:
        return "<unbound>"
    type_address = memory.pointer(address + offsets.pyobject_ob_type)
    type_name = _read_c_string(memory, memory.pointer(type_address + offsets.type_tp_name))
    if type_name == "NoneType":
        return None
    if type_name in {"bool", "int"}:
        value = _read_long(memory, offsets, address)
        return bool(value) if type_name == "bool" else value
    if type_name == "float":
        return struct.unpack("<d", memory.read(address + offsets.float_ob_fval, 8))[0]
    if type_name == "str":
        return _read_unicode(memory, offsets, address)
    if type_name == "bytes":
        return _read_bytes(memory, offsets, address)
    return _UnsupportedValue(type_name)


@dataclass(frozen=True)
class _UnsupportedValue:
    type_name: str


def _read_long(memory: _RemoteMemory, offsets: _DebugOffsets, address: int) -> int:
    tag = memory.pointer(address + offsets.long_lv_tag)
    sign_code = tag & 0x3
    count = tag >> 3
    if sign_code not in {0, 1, 2} or count > _MAX_INTEGER_DIGITS:
        raise LocalReadError("target integer exceeds supported bounds")
    if sign_code == 1:
        return 0
    raw = memory.read(address + offsets.long_ob_digit, max(count, 1) * 4)
    value = sum(
        struct.unpack_from("<I", raw, index * 4)[0] << (_PYLONG_SHIFT * index)
        for index in range(count)
    )
    return -value if sign_code == 2 else value


def _read_c_string(memory: _RemoteMemory, address: int) -> str:
    raw = memory.read(address, 128)
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


def _u64(data: bytes, offset: int) -> int:
    if offset + 8 > len(data):
        raise LocalReadError("target layout points outside a validated object")
    return struct.unpack_from("<Q", data, offset)[0]


def _u8(data: bytes, offset: int) -> int:
    if offset >= len(data):
        raise LocalReadError("target layout points outside a validated object")
    return data[offset]
