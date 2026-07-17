from __future__ import annotations

import unittest
from unittest.mock import patch
from threading import Event, Thread
import time

from prototype.adapter.mixed_stack import MixedStackHooks
from prototype.adapter.proxy import ProxyContext
from prototype.adapter.state import ProxySessionState
from prototype.python.pyrust_stack import Frame, StackReadError, ThreadStack


class _ContextProxy:
    def __init__(self, native_frames: list[dict[str, object]]) -> None:
        self.native_frames = native_frames
        self.outputs: list[str] = []
        self.stack_arguments: list[dict[str, object]] = []

    def request_downstream(
        self,
        command: str,
        arguments: dict[str, object] | None,
        *,
        timeout: float | None,
    ) -> dict[str, object]:
        assert arguments is not None
        self.stack_arguments.append(dict(arguments))
        return {
            "success": True,
            "body": {
                "stackFrames": self.native_frames,
                "totalFrames": len(self.native_frames),
            },
        }

    def send_upstream(self, message: dict[str, object]) -> None:
        self.outputs.append(str((message.get("body") or {}).get("output", "")))


class MixedStackHooksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.native = [
            {"id": 101, "name": "pyrust_native::rust_inner"},
            {"id": 102, "name": "pyrust_native::rust_outer"},
            {"id": 103, "name": "_PyEval_Vector"},
        ]
        self.state = ProxySessionState()
        self.state.on_stopped()
        self.proxy = _ContextProxy(self.native)
        self.context = ProxyContext(self.proxy, self.state)  # type: ignore[arg-type]
        self.hooks = MixedStackHooks()
        self.hooks.on_stopped(
            {"body": {"threadId": 77}},
            self.context,
        )

    def test_merges_and_pages_after_inserting_python_frames(self) -> None:
        stacks = (
            ThreadStack(
                thread_id=77,
                frames=(
                    Frame("python_inner", "/work/app.py", 5),
                    Frame("python_outer", "/work/app.py", 9),
                ),
            ),
        )
        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            return_value=stacks,
        ):
            response = self.hooks.on_stack_trace(
                {
                    "arguments": {
                        "threadId": 77,
                        "startFrame": 1,
                        "levels": 2,
                        "format": {"hex": True},
                    }
                },
                self.context,
            )

        self.assertTrue(response.success)
        assert response.body is not None
        self.assertEqual(response.body["totalFrames"], 5)
        self.assertEqual(
            [frame["name"] for frame in response.body["stackFrames"]],
            ["pyrust_native::rust_outer", "python_inner"],
        )
        self.assertEqual(response.body["stackFrames"][0]["id"], 102)
        self.assertEqual(
            self.proxy.stack_arguments,
            [{"threadId": 77, "format": {"hex": True}}],
        )

    def test_helper_failure_returns_native_stack_and_one_diagnostic(self) -> None:
        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            side_effect=StackReadError("unwinder_failure"),
        ):
            first = self.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                self.context,
            )
            second = self.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                self.context,
            )

        self.assertTrue(first.success)
        self.assertEqual(first.body, {"stackFrames": self.native, "totalFrames": 3})
        self.assertEqual(second.body, first.body)
        self.assertEqual(len(self.proxy.outputs), 1)
        self.assertIn("helper failure", self.proxy.outputs[0].lower())

    def test_in_process_unwind_timeout_returns_native_stack(self) -> None:
        self.hooks.on_launch(
            {
                "arguments": {
                    "pyrustHelperTimeoutMs": 20,
                }
            },
            self.context,
        )

        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            side_effect=lambda pid: time.sleep(1),
        ):
            started = time.monotonic()
            response = self.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                self.context,
            )

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(
            response.body,
            {"stackFrames": self.native, "totalFrames": 3},
        )
        self.assertIn("timeout", self.proxy.outputs[0].lower())

    def test_late_stack_collection_cannot_allocate_in_a_new_epoch(self) -> None:
        entered = Event()
        release = Event()
        result: list[object] = []
        stacks = (
            ThreadStack(
                thread_id=77,
                frames=(Frame("python_inner", "/work/app.py", 5),),
            ),
        )

        def collect(pid: int) -> tuple[ThreadStack, ...]:
            entered.set()
            release.wait(2)
            return stacks

        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            side_effect=collect,
        ):
            worker = Thread(
                target=lambda: result.append(
                    self.hooks.on_stack_trace(
                        {"arguments": {"threadId": 77}},
                        self.context,
                    )
                )
            )
            worker.start()
            self.assertTrue(entered.wait(1))
            self.state.on_continued()
            self.state.on_stopped()
            release.set()
            worker.join(2)

        self.assertFalse(worker.is_alive())
        response = result[0]
        self.assertFalse(response.success)  # type: ignore[union-attr]
        self.assertIn("continued", response.message)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
