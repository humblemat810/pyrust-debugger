"""Stop-epoch and synthetic-frame state shared with proxy hooks."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any, Hashable, Iterable, Literal


MAX_DAP_ID = 2_147_483_647


@dataclass(frozen=True)
class SyntheticFrame:
    frame_id: int
    epoch: int
    thread_id: int
    key: Hashable
    value: Any


class SyntheticFrameRegistry:
    """Allocate stable-in-epoch IDs and distinguish stale synthetic frames."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._epoch = 0
        self._next_id = MAX_DAP_ID
        self._by_key: dict[tuple[int, Hashable], SyntheticFrame] = {}
        self._by_id: dict[int, SyntheticFrame] = {}
        self._issued_ids: set[int] = set()
        self._reserved_native_ids: set[int] = set()

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def begin_epoch(self, epoch: int) -> None:
        with self._lock:
            self._epoch = epoch
            self._by_key.clear()
            self._by_id.clear()
            self._reserved_native_ids.clear()

    def clear_current(self) -> None:
        with self._lock:
            self._by_key.clear()
            self._by_id.clear()
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
    ) -> int:
        with self._lock:
            if expected_epoch is not None and self._epoch != expected_epoch:
                raise RuntimeError(
                    f"synthetic frame epoch changed from {expected_epoch} "
                    f"to {self._epoch}"
                )
            self._reserved_native_ids.update(native_frame_ids)
            lookup_key = (thread_id, key)
            existing = self._by_key.get(lookup_key)
            if existing is not None:
                return existing.frame_id

            frame_id = self._allocate_id()
            frame = SyntheticFrame(
                frame_id=frame_id,
                epoch=self._epoch,
                thread_id=thread_id,
                key=key,
                value=value,
            )
            self._by_key[lookup_key] = frame
            self._by_id[frame_id] = frame
            self._issued_ids.add(frame_id)
            return frame_id

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
    """Minimal process and stop state exposed to integration hooks."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._stop_epoch = 0
        self._is_stopped = False
        self._process_id: int | None = None
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
            return self._process_id

    def record_process_event(self, event: dict[str, Any]) -> None:
        process_id = event.get("body", {}).get("systemProcessId")
        if isinstance(process_id, int) and process_id > 0:
            with self._lock:
                self._process_id = process_id

    def on_stopped(self) -> int:
        with self._lock:
            self._stop_epoch += 1
            self._is_stopped = True
            epoch = self._stop_epoch
        self.synthetic_frames.begin_epoch(epoch)
        return epoch

    def on_continued(self) -> None:
        with self._lock:
            self._is_stopped = False
        self.synthetic_frames.clear_current()

    def on_terminated(self) -> None:
        with self._lock:
            self._is_stopped = False
            self._process_id = None
        self.synthetic_frames.clear_current()
