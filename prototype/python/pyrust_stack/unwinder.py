"""Python 3.14 remote stack reader used by the DAP proxy prototype."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import sys
from typing import Any

try:
    from _remote_debugging import RemoteUnwinder
except ImportError as error:  # pragma: no cover - only reachable before 3.14.
    raise RuntimeError("pyrust_stack requires CPython 3.14") from error


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
    """Read every Python thread stack from a CPython 3.14 process.

    CPython reads the target's exported debug-offset metadata and process
    memory. The target does not need to execute code, so this also works while
    a native debugger has stopped it at a Rust breakpoint.
    """
    if sys.version_info[:2] != (3, 14):
        raise RuntimeError(
            f"pyrust_stack requires CPython 3.14, got {sys.version_info.major}."
            f"{sys.version_info.minor}"
        )

    threads = RemoteUnwinder(pid, all_threads=True).get_stack_trace()
    return tuple(
        ThreadStack(
            thread_id=thread.thread_id,
            frames=tuple(
                Frame(
                    name=frame.funcname,
                    path=frame.filename,
                    line=frame.lineno,
                )
                for frame in thread.frame_info
            ),
        )
        for thread in threads
    )
