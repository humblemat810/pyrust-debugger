"""Process, thread, stop-epoch, and synthetic-frame state for proxy hooks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from threading import Lock
from typing import Any, Hashable, Iterable, Literal

from .coordinator import (
    CoordinationError,
    DebugEngine,
    PROCESS_COMMAND_UNAVAILABLE,
    ProcessCoordinator,
    bounded_process_command,
)

MAX_DAP_ID = 2_147_483_647


@dataclass(frozen=True)
class SyntheticFrame:
    frame_id: int
    epoch: int
    process_id: int | None
    thread_id: int
    key: Hashable
    value: Any


@dataclass(frozen=True)
class ProcessSnapshot:
    """Coordinator-visible state for one debuggee process."""

    process_id: int
    stop_epoch: int
    is_stopped: bool
    thread_ids: frozenset[int]
    stopped_thread_ids: frozenset[int] = frozenset()
    all_threads_stopped: bool = False


class SyntheticFrameRegistry:
    """Allocate stable-in-epoch IDs and distinguish stale synthetic frames."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._epoch = 0
        self._process_epochs: dict[int | None, int] = {}
        self._next_id = MAX_DAP_ID
        self._by_key: dict[tuple[int | None, int, Hashable], SyntheticFrame] = {}
        self._by_id: dict[int, SyntheticFrame] = {}
        self._issued_ids: set[int] = set()
        self._reserved_native_ids: set[int] = set()

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def begin_epoch(
        self,
        epoch: int,
        *,
        process_id: int | None = None,
        thread_id: int | None = None,
    ) -> None:
        with self._lock:
            self._epoch = epoch
            self._process_epochs[process_id] = epoch
            self._discard_current_process(process_id, thread_id)
            self._reserved_native_ids.clear()

    def clear_current(
        self,
        *,
        process_id: int | None = None,
        thread_id: int | None = None,
    ) -> None:
        with self._lock:
            self._discard_current_process(process_id, thread_id)
            self._reserved_native_ids.clear()

    def reserve_native_ids(self, frame_ids: Iterable[int]) -> None:
        with self._lock:
            self._reserved_native_ids.update(frame_ids)

    def allocate(
        self,
        thread_id: int,
        key: Hashable,
        value: Any,
        *,
        native_frame_ids: Iterable[int] = (),
        expected_epoch: int | None = None,
        process_id: int | None = None,
    ) -> int:
        with self._lock:
            active_epoch = self._process_epochs.get(process_id, self._epoch)
            if expected_epoch is not None and active_epoch != expected_epoch:
                raise RuntimeError(
                    f"synthetic frame epoch changed from {expected_epoch} to "
                    f"{active_epoch}"
                )
            self._reserved_native_ids.update(native_frame_ids)
            lookup_key = (process_id, thread_id, key)
            existing = self._by_key.get(lookup_key)
            if existing is not None:
                return existing.frame_id

            frame_id = self._allocate_id()
            frame = SyntheticFrame(
                frame_id=frame_id,
                epoch=active_epoch,
                process_id=process_id,
                thread_id=thread_id,
                key=key,
                value=value,
            )
            self._by_key[lookup_key] = frame
            self._by_id[frame_id] = frame
            self._issued_ids.add(frame_id)
            return frame_id

    def _discard_current_process(
        self,
        process_id: int | None,
        thread_id: int | None,
    ) -> None:
        frame_ids = [
            frame_id
            for frame_id, frame in self._by_id.items()
            if frame.process_id == process_id
            and (thread_id is None or frame.thread_id == thread_id)
        ]
        for frame_id in frame_ids:
            frame = self._by_id.pop(frame_id)
            self._by_key.pop((frame.process_id, frame.thread_id, frame.key), None)

    def get(self, frame_id: int) -> SyntheticFrame | None:
        with self._lock:
            return self._by_id.get(frame_id)

    def classify(self, frame_id: int) -> Literal["current", "stale", "native"]:
        with self._lock:
            if frame_id in self._reserved_native_ids:
                return "native"
            if frame_id in self._by_id:
                return "current"
            if frame_id in self._issued_ids:
                return "stale"
            return "native"

    def _allocate_id(self) -> int:
        while self._next_id > 0:
            candidate = self._next_id
            self._next_id -= 1
            if (
                candidate not in self._issued_ids
                and candidate not in self._reserved_native_ids
            ):
                return candidate
        raise RuntimeError("synthetic DAP frame ID space exhausted")


class ProxySessionState:
    """Coordinator-ready process and stop state exposed to integration hooks.

    The current proxy still owns one CodeLLDB transport, but state is keyed by
    process and thread so a coordinator can safely grow to multiple native and
    Python sessions without reusing the single-process assumptions.
    """

    def __init__(self, coordinator: ProcessCoordinator | None = None) -> None:
        self._lock = Lock()
        self._stop_epoch = 0
        self._is_stopped = False
        self._active_process_id: int | None = None
        self._default_process_name: str | None = None
        self._default_process_role: str | None = None
        self._default_process_command: str | None = None
        self._processes: dict[int, ProcessSnapshot] = {}
        self._thread_processes: dict[int, int] = {}
        self._thread_names: dict[int, str] = {}
        self.coordinator = coordinator or ProcessCoordinator()
        self.synthetic_frames = SyntheticFrameRegistry()

    @property
    def stop_epoch(self) -> int:
        with self._lock:
            return self._stop_epoch

    @property
    def is_stopped(self) -> bool:
        with self._lock:
            return self._is_stopped

    @property
    def process_id(self) -> int | None:
        with self._lock:
            return self._active_process_id

    @property
    def process_ids(self) -> frozenset[int]:
        with self._lock:
            return frozenset(self._processes)

    def record_process_event(self, event: dict[str, Any]) -> None:
        body = event.get("body") or {}
        process_id = body.get("systemProcessId")
        parent_process_id = body.get("parentProcessId")
        if _valid_id(process_id):
            self.register_process(
                process_id,
                parent_process_id=(
                    parent_process_id if _valid_id(parent_process_id) else None
                ),
            )

    def record_output_event(self, event: dict[str, Any]) -> None:
        """Capture CodeLLDB's launch PID fallback until it emits process events."""

        output = (event.get("body") or {}).get("output")
        if not isinstance(output, str):
            return
        match = re.search(r"\bLaunched process (\d+)\b", output)
        if match:
            self.register_process(int(match.group(1)))

    def set_default_process_metadata(
        self,
        label: str,
        role: str,
        command: str | None = None,
    ) -> None:
        """Use launch metadata until the native adapter reports a process ID."""

        with self._lock:
            self._default_process_name = label
            self._default_process_role = role
            self._default_process_command = bounded_process_command(command)

    def register_process(
        self,
        process_id: int,
        *,
        parent_process_id: int | None = None,
        display_name: str | None = None,
        role: str | None = None,
        command: str | None = None,
        inherit_default_metadata: bool = True,
        engine: DebugEngine = "native",
    ) -> None:
        if not _valid_id(process_id):
            return
        with self._lock:
            label = display_name or (
                self._default_process_name if inherit_default_metadata else None
            )
            process_role = role or (
                self._default_process_role if inherit_default_metadata else None
            )
            process_command = (
                command
                if command is not None
                else (
                    self._default_process_command
                    if inherit_default_metadata
                    else None
                )
            )
            existing = self._processes.get(process_id)
            self._processes[process_id] = existing or ProcessSnapshot(
                process_id=process_id,
                stop_epoch=0,
                is_stopped=False,
                thread_ids=frozenset(),
            )
            self._active_process_id = process_id
        self.coordinator.register_process(
            process_id,
            parent_process_id=parent_process_id,
            engine=engine,
            display_name=label,
            role=process_role,
            command=process_command,
        )

    def record_threads_response(self, response: dict[str, Any]) -> None:
        """Associate the current native process with its reported DAP threads."""

        threads = (response.get("body") or {}).get("threads")
        if not isinstance(threads, list):
            return
        with self._lock:
            process_id = self._active_process_id
        if process_id is None:
            return
        for thread in threads:
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if _valid_id(thread_id):
                self.bind_thread(
                    process_id,
                    thread_id,
                    name=thread.get("name") if isinstance(thread.get("name"), str) else None,
                )

    def bind_thread(
        self,
        process_id: int,
        thread_id: int,
        *,
        name: str | None = None,
        activate: bool = True,
    ) -> None:
        if not _valid_id(process_id) or not _valid_id(thread_id):
            return
        with self._lock:
            existing = self._processes.get(process_id)
            if existing is None:
                existing = ProcessSnapshot(
                    process_id=process_id,
                    stop_epoch=0,
                    is_stopped=False,
                    thread_ids=frozenset(),
                )
            self._processes[process_id] = ProcessSnapshot(
                process_id=process_id,
                stop_epoch=existing.stop_epoch,
                is_stopped=existing.is_stopped,
                thread_ids=existing.thread_ids | {thread_id},
                stopped_thread_ids=(
                    existing.stopped_thread_ids | {thread_id}
                    if existing.all_threads_stopped
                    else existing.stopped_thread_ids
                ),
                all_threads_stopped=existing.all_threads_stopped,
            )
            self._thread_processes[thread_id] = process_id
            if name:
                self._thread_names[thread_id] = name
            if activate:
                self._active_process_id = process_id
        self.coordinator.bind_native_thread(process_id, thread_id, name=name)

    def bind_python_thread(
        self,
        process_id: int,
        thread_id: int,
        *,
        name: str | None = None,
        activate: bool = True,
    ) -> None:
        """Register a PyRust-virtualized debugpy thread for one process."""

        if not _valid_id(process_id) or not _valid_id(thread_id):
            return
        with self._lock:
            existing = self._processes.get(process_id)
            if existing is None:
                existing = ProcessSnapshot(
                    process_id=process_id,
                    stop_epoch=0,
                    is_stopped=False,
                    thread_ids=frozenset(),
                )
            self._processes[process_id] = ProcessSnapshot(
                process_id=process_id,
                stop_epoch=existing.stop_epoch,
                is_stopped=existing.is_stopped,
                thread_ids=existing.thread_ids | {thread_id},
                stopped_thread_ids=(
                    existing.stopped_thread_ids | {thread_id}
                    if existing.all_threads_stopped
                    else existing.stopped_thread_ids
                ),
                all_threads_stopped=existing.all_threads_stopped,
            )
            self._thread_processes[thread_id] = process_id
            if name:
                self._thread_names[thread_id] = name
            if activate:
                self._active_process_id = process_id
        self.coordinator.register_process(process_id, engine="python")

    def process_id_for_thread(self, thread_id: int) -> int | None:
        with self._lock:
            return self._thread_processes.get(thread_id) or self._active_process_id

    def process_snapshot(self, process_id: int) -> ProcessSnapshot | None:
        with self._lock:
            return self._processes.get(process_id)

    def process_tree(self) -> list[dict[str, Any]]:
        """Expose coordinator-owned process/thread hierarchy to the extension."""

        with self._lock:
            snapshots = dict(self._processes)
            active_process_id = self._active_process_id
        processes: list[dict[str, Any]] = []
        for session in self.coordinator.processes():
            snapshot = snapshots.get(session.process_id)
            thread_ids = (
                snapshot.thread_ids
                if snapshot is not None
                else frozenset(session.threads)
            )
            processes.append(
                {
                    "processId": session.process_id,
                    "parentProcessId": session.parent_process_id,
                    "label": session.display_name
                    or f"Process {session.process_id}",
                    "role": session.role or "native process",
                    "command": session.command or PROCESS_COMMAND_UNAVAILABLE,
                    "isActive": session.process_id == active_process_id,
                    "isStopped": bool(snapshot and snapshot.is_stopped),
                    "threads": [
                        {
                            "threadId": thread_id,
                            "name": (
                                (
                                    session.threads.get(thread_id).name
                                    if session.threads.get(thread_id) is not None
                                    else self._thread_names.get(thread_id)
                                )
                                or f"Thread {thread_id}"
                            ),
                            "isStopped": bool(
                                snapshot
                                and (
                                    snapshot.all_threads_stopped
                                    or thread_id in snapshot.stopped_thread_ids
                                )
                            ),
                        }
                        for thread_id in sorted(thread_ids)
                    ],
                }
            )
        return processes

    def is_thread_stopped(self, thread_id: int) -> bool:
        process_id = self.process_id_for_thread(thread_id)
        if process_id is None:
            return False
        with self._lock:
            snapshot = self._processes.get(process_id)
            return bool(snapshot and thread_id in snapshot.stopped_thread_ids)

    def remove_process(self, process_id: int) -> None:
        """Forget one child without disrupting unrelated process sessions."""

        if not _valid_id(process_id):
            return
        with self._lock:
            self._processes.pop(process_id, None)
            removed_thread_ids = {
                thread_id
                for thread_id, owner in self._thread_processes.items()
                if owner == process_id
            }
            self._thread_processes = {
                thread_id: owner
                for thread_id, owner in self._thread_processes.items()
                if owner != process_id
            }
            self._thread_names = {
                thread_id: name
                for thread_id, name in self._thread_names.items()
                if thread_id not in removed_thread_ids
            }
            if self._active_process_id == process_id:
                self._active_process_id = next(iter(self._processes), None)
            self._is_stopped = any(
                snapshot.is_stopped for snapshot in self._processes.values()
            )
        self.coordinator.remove_process(process_id)

    def on_stopped(
        self,
        event: dict[str, Any] | None = None,
        *,
        owner: DebugEngine = "native",
    ) -> int:
        thread_id = (event.get("body") or {}).get("threadId") if event else None
        event_process_id = (
            (event.get("body") or {}).get("systemProcessId") if event else None
        )
        with self._lock:
            self._stop_epoch += 1
            self._is_stopped = True
            epoch = self._stop_epoch
            process_id = (
                event_process_id
                if _valid_id(event_process_id)
                else (
                    self._thread_processes.get(thread_id)
                    if _valid_id(thread_id)
                    else self._active_process_id
                )
            )
            if process_id is None and _valid_id(thread_id):
                # A direct CodeLLDB launch can stop before emitting either a
                # process event or launch-output PID. The stopped DAP thread
                # remains the only authoritative identity in that narrow gap.
                process_id = _thread_group_id(thread_id) or thread_id
                self._processes[process_id] = ProcessSnapshot(
                    process_id=process_id,
                    stop_epoch=0,
                    is_stopped=False,
                    thread_ids=frozenset(),
                )
            if process_id is not None:
                existing = self._processes.get(process_id)
                self._processes[process_id] = ProcessSnapshot(
                    process_id=process_id,
                    stop_epoch=epoch,
                    is_stopped=True,
                    thread_ids=(
                        existing.thread_ids | {thread_id}
                        if existing is not None and _valid_id(thread_id)
                        else (
                            frozenset({thread_id})
                            if _valid_id(thread_id)
                            else (existing.thread_ids if existing else frozenset())
                        )
                    ),
                    stopped_thread_ids=(
                        existing.stopped_thread_ids | {thread_id}
                        if existing is not None and _valid_id(thread_id)
                        else (
                            frozenset({thread_id})
                            if _valid_id(thread_id)
                            else (
                                existing.stopped_thread_ids
                                if existing
                                else frozenset()
                            )
                        )
                    ),
                    all_threads_stopped=bool(
                        (event.get("body") or {}).get("allThreadsStopped")
                        if event
                        else False
                    ),
                )
                if _valid_id(thread_id):
                    self._thread_processes[thread_id] = process_id
                self._active_process_id = process_id
        if process_id is not None and _valid_id(thread_id):
            try:
                self.coordinator.register_process(process_id, engine=owner)
                if owner == "native":
                    self.coordinator.bind_native_thread(process_id, thread_id)
                self.coordinator.acquire_stop(process_id, thread_id, owner)
            except CoordinationError:
                # A future Python-owned stop must not be overwritten by a
                # native event. The hook will fall back rather than guessing.
                pass
        self.synthetic_frames.begin_epoch(
            epoch,
            process_id=process_id,
            thread_id=thread_id if _valid_id(thread_id) else None,
        )
        # Older hook tests and third-party hooks can still allocate frames
        # without an owning process. Keep that legacy namespace epoch-scoped.
        self.synthetic_frames.begin_epoch(epoch, process_id=None)
        return epoch

    def on_continued(
        self,
        event: dict[str, Any] | None = None,
        *,
        owner: DebugEngine = "native",
    ) -> None:
        event_process_id = (
            (event.get("body") or {}).get("systemProcessId") if event else None
        )
        process_still_stopped = False
        with self._lock:
            thread_id = (event.get("body") or {}).get("threadId") if event else None
            process_id = (
                event_process_id
                if _valid_id(event_process_id)
                else (
                    self._thread_processes.get(thread_id)
                    if _valid_id(thread_id)
                    else self._active_process_id
                )
            )
            if process_id is not None and process_id in self._processes:
                existing = self._processes[process_id]
                all_threads_continued = bool(
                    (event.get("body") or {}).get("allThreadsContinued")
                    if event
                    else False
                )
                stopped_thread_ids = (
                    frozenset()
                    if all_threads_continued or not _valid_id(thread_id)
                    else (
                        existing.thread_ids - {thread_id}
                        if existing.all_threads_stopped
                        else existing.stopped_thread_ids - {thread_id}
                    )
                )
                self._processes[process_id] = ProcessSnapshot(
                    process_id=process_id,
                    stop_epoch=existing.stop_epoch,
                    is_stopped=bool(stopped_thread_ids),
                    thread_ids=existing.thread_ids,
                    stopped_thread_ids=stopped_thread_ids,
                    all_threads_stopped=False,
                )
                process_still_stopped = bool(stopped_thread_ids)
            self._is_stopped = any(
                snapshot.is_stopped for snapshot in self._processes.values()
            )
        if process_id is not None and not process_still_stopped:
            try:
                self.coordinator.release_stop(process_id, owner)
            except CoordinationError:
                pass
        self.synthetic_frames.clear_current(
            process_id=process_id,
            thread_id=thread_id if _valid_id(thread_id) else None,
        )
        self.synthetic_frames.clear_current(process_id=None)

    def on_terminated(self, event: dict[str, Any] | None = None) -> None:
        process_id = (
            (event.get("body") or {}).get("systemProcessId") if event else None
        )
        with self._lock:
            self._is_stopped = False
            if _valid_id(process_id):
                self._processes.pop(process_id, None)
                self._thread_processes = {
                    thread_id: owner
                    for thread_id, owner in self._thread_processes.items()
                    if owner != process_id
                }
                if self._active_process_id == process_id:
                    self._active_process_id = next(iter(self._processes), None)
            else:
                self._active_process_id = None
                self._processes.clear()
                self._thread_processes.clear()
        if _valid_id(process_id):
            self.remove_process(process_id)
            self.synthetic_frames.clear_current(process_id=process_id)
        else:
            for registered_process_id in self.coordinator.process_ids():
                self.coordinator.remove_process(registered_process_id)
        if not _valid_id(process_id):
            self.synthetic_frames.clear_current()


def _valid_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _thread_group_id(thread_id: int) -> int | None:
    """Resolve a Linux native thread to its process leader without guessing."""

    try:
        for line in Path(f"/proc/{thread_id}/status").read_text(
            encoding="utf-8"
        ).splitlines():
            if line.startswith("Tgid:"):
                value = int(line.partition(":")[2].strip())
                return value if value > 0 else None
    except (OSError, ValueError):
        return None
    return None
