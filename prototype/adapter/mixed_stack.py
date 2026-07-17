"""Fixture-bound Python/Rust stack augmentation for the first workable slice."""

from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Empty, Queue
import shlex
import subprocess
from threading import Lock, Thread
from typing import Any, Mapping

from prototype.python.pyrust_stack import StackReadError, read_python_stacks

from .proxy import LocalResponse, Message, ProxyContext, ProxyHooks


class HelperFailure(RuntimeError):
    """A bounded Python-stack collection failure."""


class HelperTimeout(HelperFailure):
    """The configured external helper exceeded its deadline."""


class StaleStopError(RuntimeError):
    """The debuggee continued while a stack request was in flight."""


class MixedStackHooks(ProxyHooks):
    """Merge one CPython stack into the fixed Python-to-Rust fixture stack."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._helper_command: str | None = None
        self._helper_timeout_ms = 1_000
        self._stopped_thread_id: int | None = None
        self._diagnosed_epochs: set[int] = set()

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
        except HelperTimeout as error:
            self._diagnose_once(context, f"PyRust helper timeout: {error}")
            merged = native_frames
        except (HelperFailure, StackReadError) as error:
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
            return [self._validate_python_frame(frame) for frame in frames]
        raise HelperFailure(f"helper returned no Python stack for thread {thread_id}")

    @staticmethod
    def _run_in_process_helper(
        process_id: int,
        timeout_ms: int,
    ) -> list[dict[str, Any]]:
        results: Queue[tuple[bool, object]] = Queue(maxsize=1)

        def collect() -> None:
            try:
                stacks = [stack.to_dict() for stack in read_python_stacks(process_id)]
            except Exception as error:
                results.put((False, error))
            else:
                results.put((True, stacks))

        Thread(
            target=collect,
            name="pyrust-cpython-unwinder",
            daemon=True,
        ).start()
        try:
            succeeded, value = results.get(timeout=timeout_ms / 1_000)
        except Empty as error:
            raise HelperTimeout(f"in-process unwind exceeded {timeout_ms} ms") from error
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
        if len(native_frames) < 2:
            raise HelperFailure("native stack is too short for fixture boundary")
        leaves = [
            str(frame.get("name", "")).rsplit("::", 1)[-1]
            for frame in native_frames[:2]
        ]
        if leaves != ["rust_inner", "rust_outer"]:
            raise HelperFailure(
                f"native fixture boundary was not found: {leaves!r}"
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
        return native_frames[:2] + synthetic + native_frames[2:]

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
