"""Shared process-tree and execution-ownership model for PyRust sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Literal


DebugEngine = Literal["native", "python"]


class CoordinationError(RuntimeError):
    """A debugger operation conflicts with the current process stop owner."""


@dataclass(frozen=True)
class ThreadBinding:
    """Native and optional Python debugger identities for one OS thread."""

    native_thread_id: int
    python_thread_id: int | None = None
    name: str | None = None


@dataclass(frozen=True)
class ExecutionLease:
    """The single debugger currently allowed to control one stopped process."""

    generation: int
    process_id: int
    thread_id: int
    owner: DebugEngine


@dataclass
class ProcessSession:
    """One process in a coordinator-owned debuggee tree."""

    process_id: int
    parent_process_id: int | None = None
    native_connected: bool = False
    python_connected: bool = False
    display_name: str | None = None
    role: str | None = None
    children: set[int] = field(default_factory=set)
    threads: dict[int, ThreadBinding] = field(default_factory=dict)


class ProcessCoordinator:
    """Coordinate process identity and stop ownership across debugger engines.

    A later multi-transport DAP coordinator can share one instance across its
    CodeLLDB and debugpy connections. No engine may independently resume a
    process while another engine owns its active stop lease.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._processes: dict[int, ProcessSession] = {}
        self._leases: dict[int, ExecutionLease] = {}
        self._next_generation = 1

    def register_process(
        self,
        process_id: int,
        *,
        parent_process_id: int | None = None,
        engine: DebugEngine | None = None,
        display_name: str | None = None,
        role: str | None = None,
    ) -> ProcessSession:
        _require_id(process_id, "process ID")
        if parent_process_id is not None:
            _require_id(parent_process_id, "parent process ID")
        with self._lock:
            session = self._processes.get(process_id)
            if session is None:
                session = ProcessSession(
                    process_id=process_id,
                    parent_process_id=parent_process_id,
                )
                self._processes[process_id] = session
            elif (
                parent_process_id is not None
                and session.parent_process_id not in {None, parent_process_id}
            ):
                raise CoordinationError(
                    f"process {process_id} was already registered under "
                    f"parent {session.parent_process_id}"
                )
            elif parent_process_id is not None:
                session.parent_process_id = parent_process_id
            if display_name:
                session.display_name = display_name
            if role:
                session.role = role

            if parent_process_id is not None:
                parent = self._processes.get(parent_process_id)
                if parent is None:
                    parent = ProcessSession(process_id=parent_process_id)
                    self._processes[parent_process_id] = parent
                parent.children.add(process_id)
            if engine == "native":
                session.native_connected = True
            elif engine == "python":
                session.python_connected = True
            return _copy_session(session)

    def bind_native_thread(
        self,
        process_id: int,
        native_thread_id: int,
        *,
        name: str | None = None,
    ) -> None:
        _require_id(native_thread_id, "native thread ID")
        with self._lock:
            session = self._require_process(process_id)
            existing = session.threads.get(native_thread_id)
            session.threads[native_thread_id] = ThreadBinding(
                native_thread_id=native_thread_id,
                python_thread_id=existing.python_thread_id if existing else None,
                name=name or (existing.name if existing else None),
            )

    def bind_python_thread(
        self,
        process_id: int,
        native_thread_id: int,
        python_thread_id: int,
    ) -> None:
        _require_id(native_thread_id, "native thread ID")
        _require_id(python_thread_id, "Python thread ID")
        with self._lock:
            session = self._require_process(process_id)
            existing = session.threads.get(native_thread_id)
            session.threads[native_thread_id] = ThreadBinding(
                native_thread_id=native_thread_id,
                python_thread_id=python_thread_id,
                name=existing.name if existing else None,
            )

    def process(self, process_id: int) -> ProcessSession | None:
        with self._lock:
            session = self._processes.get(process_id)
            return _copy_session(session) if session else None

    def process_ids(self) -> frozenset[int]:
        with self._lock:
            return frozenset(self._processes)

    def processes(self) -> tuple[ProcessSession, ...]:
        """Return a stable snapshot for the VS Code process-tree view."""

        with self._lock:
            return tuple(
                _copy_session(self._processes[process_id])
                for process_id in sorted(self._processes)
            )

    def acquire_stop(
        self,
        process_id: int,
        thread_id: int,
        owner: DebugEngine,
    ) -> ExecutionLease:
        _require_id(thread_id, "thread ID")
        with self._lock:
            self._require_process(process_id)
            existing = self._leases.get(process_id)
            if existing is not None and existing.owner != owner:
                raise CoordinationError(
                    f"process {process_id} is already stopped under "
                    f"{existing.owner} control"
                )
            lease = ExecutionLease(
                generation=self._next_generation,
                process_id=process_id,
                thread_id=thread_id,
                owner=owner,
            )
            self._next_generation += 1
            self._leases[process_id] = lease
            return lease

    def release_stop(
        self,
        process_id: int,
        owner: DebugEngine,
    ) -> None:
        with self._lock:
            existing = self._leases.get(process_id)
            if existing is None:
                return
            if existing.owner != owner:
                raise CoordinationError(
                    f"{owner} cannot resume process {process_id}; "
                    f"{existing.owner} owns its stop"
                )
            self._leases.pop(process_id, None)

    def execution_owner(self, process_id: int) -> DebugEngine | None:
        with self._lock:
            lease = self._leases.get(process_id)
            return lease.owner if lease else None

    def remove_process(self, process_id: int) -> None:
        with self._lock:
            session = self._processes.pop(process_id, None)
            self._leases.pop(process_id, None)
            if session and session.parent_process_id in self._processes:
                self._processes[session.parent_process_id].children.discard(process_id)

    def _require_process(self, process_id: int) -> ProcessSession:
        _require_id(process_id, "process ID")
        session = self._processes.get(process_id)
        if session is None:
            raise CoordinationError(f"process {process_id} is not registered")
        return session


def _copy_session(session: ProcessSession) -> ProcessSession:
    return ProcessSession(
        process_id=session.process_id,
        parent_process_id=session.parent_process_id,
        native_connected=session.native_connected,
        python_connected=session.python_connected,
        display_name=session.display_name,
        role=session.role,
        children=set(session.children),
        threads=dict(session.threads),
    )


def _require_id(value: object, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
