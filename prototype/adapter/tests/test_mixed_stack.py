from __future__ import annotations

import unittest
from unittest.mock import patch
from threading import Event, Lock, Thread
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

    def test_python_outer_golden_merge_preserves_native_frames(self) -> None:
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
                {"arguments": {"threadId": 77}},
                self.context,
            )

        self.assertTrue(response.success)
        self.assertEqual(
            response.body,
            {
                "stackFrames": [
                    self.native[0],
                    self.native[1],
                    {
                        "id": 2_147_483_647,
                        "name": "python_inner",
                        "source": {"name": "app.py", "path": "/work/app.py"},
                        "line": 5,
                        "column": 1,
                        "presentationHint": "normal",
                    },
                    {
                        "id": 2_147_483_646,
                        "name": "python_outer",
                        "source": {"name": "app.py", "path": "/work/app.py"},
                        "line": 9,
                        "column": 1,
                        "presentationHint": "normal",
                    },
                    self.native[2],
                ],
                "totalFrames": 5,
            },
        )

    def test_rust_outer_golden_merge_preserves_all_native_frames(self) -> None:
        reverse_native = [
            {"id": 201, "name": "rust_outer_python_inner::rust_callback"},
            {"id": 202, "name": "rust_outer_python_inner::__pyfunction_rust_callback"},
            {"id": 203, "name": "_PyFunction_Vectorcall"},
            {
                "id": 204,
                "name": (
                    "<pyo3::instance::Bound<pyo3::types::any::PyAny> "
                    "as pyo3::types::any::PyAnyMethods>::call0"
                ),
            },
            {
                "id": 205,
                "name": "rust_outer_python_inner::rust_outer::{closure#0}",
            },
            {"id": 206, "name": "pyo3::marker::Python::attach"},
            {"id": 207, "name": "rust_outer_python_inner::rust_outer"},
            {"id": 208, "name": "rust_outer_python_inner::main"},
            {"id": 209, "name": "__libc_start_main_impl"},
        ]
        self.proxy.native_frames = reverse_native
        stacks = (
            ThreadStack(
                thread_id=77,
                frames=(
                    Frame("python_inner", "/work/embedded.py", 2),
                    Frame("python_outer", "/work/embedded.py", 6),
                ),
            ),
        )

        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            return_value=stacks,
        ):
            response = self.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                self.context,
            )

        self.assertTrue(response.success)
        self.assertEqual(
            response.body,
            {
                "stackFrames": [
                    reverse_native[0],
                    {
                        "id": 2_147_483_647,
                        "name": "python_inner",
                        "source": {
                            "name": "embedded.py",
                            "path": "/work/embedded.py",
                        },
                        "line": 2,
                        "column": 1,
                        "presentationHint": "normal",
                    },
                    {
                        "id": 2_147_483_646,
                        "name": "python_outer",
                        "source": {
                            "name": "embedded.py",
                            "path": "/work/embedded.py",
                        },
                        "line": 6,
                        "column": 1,
                        "presentationHint": "normal",
                    },
                    *reverse_native[1:],
                ],
                "totalFrames": 11,
            },
        )
        assert response.body is not None
        frames = response.body["stackFrames"]
        self.assertEqual(
            [frame["id"] for frame in frames if frame["id"] < 1_000],
            [frame["id"] for frame in reverse_native],
        )

    def test_rust_outer_pages_after_crossing_both_language_boundaries(
        self,
    ) -> None:
        reverse_native = [
            {"id": 201, "name": "rust_outer_python_inner::rust_callback"},
            {"id": 207, "name": "rust_outer_python_inner::rust_outer"},
            {"id": 208, "name": "rust_outer_python_inner::main"},
        ]
        self.proxy.native_frames = reverse_native
        stacks = (
            ThreadStack(
                thread_id=77,
                frames=(
                    Frame("python_inner", "/work/embedded.py", 2),
                    Frame("python_outer", "/work/embedded.py", 6),
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
                        "startFrame": 0,
                        "levels": 4,
                    }
                },
                self.context,
            )

        self.assertTrue(response.success)
        assert response.body is not None
        self.assertEqual(response.body["totalFrames"], 5)
        self.assertEqual(
            [frame["name"] for frame in response.body["stackFrames"]],
            [
                "rust_outer_python_inner::rust_callback",
                "python_inner",
                "python_outer",
                "rust_outer_python_inner::rust_outer",
            ],
        )
        self.assertEqual(self.proxy.stack_arguments, [{"threadId": 77}])

    def test_rust_outer_allows_an_embedded_module_frame(self) -> None:
        reverse_native = [
            {"id": 201, "name": "rust_outer_python_inner::rust_callback"},
            {"id": 207, "name": "rust_outer_python_inner::rust_outer"},
            {"id": 208, "name": "rust_outer_python_inner::main"},
        ]
        self.proxy.native_frames = reverse_native
        stacks = (
            ThreadStack(
                thread_id=77,
                frames=(
                    Frame("python_inner", "/work/embedded.py", 2),
                    Frame("python_outer", "/work/embedded.py", 6),
                    Frame("<module>", "/work/embedded.py", 9),
                ),
            ),
        )

        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            return_value=stacks,
        ):
            response = self.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                self.context,
            )

        self.assertTrue(response.success)
        assert response.body is not None
        self.assertEqual(
            [frame["name"] for frame in response.body["stackFrames"]],
            [
                "rust_outer_python_inner::rust_callback",
                "python_inner",
                "python_outer",
                "<module>",
                "rust_outer_python_inner::rust_outer",
                "rust_outer_python_inner::main",
            ],
        )

    def test_unknown_rust_outer_boundary_returns_original_native_stack(
        self,
    ) -> None:
        unknown_native = [
            {"id": 201, "name": "rust_outer_python_inner::rust_callback"},
            {"id": 202, "name": "_PyFunction_Vectorcall"},
            {"id": 208, "name": "rust_outer_python_inner::main"},
        ]
        self.proxy.native_frames = unknown_native
        stacks = (
            ThreadStack(
                thread_id=77,
                frames=(
                    Frame("python_inner", "/work/embedded.py", 2),
                    Frame("python_outer", "/work/embedded.py", 6),
                ),
            ),
        )

        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            return_value=stacks,
        ):
            response = self.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                self.context,
            )

        self.assertTrue(response.success)
        self.assertEqual(
            response.body,
            {"stackFrames": unknown_native, "totalFrames": 3},
        )
        self.assertEqual(len(self.proxy.outputs), 1)
        self.assertIn("helper failure", self.proxy.outputs[0].lower())

    def test_incomplete_rust_outer_python_boundary_returns_native_stack(
        self,
    ) -> None:
        reverse_native = [
            {"id": 201, "name": "rust_outer_python_inner::rust_callback"},
            {"id": 207, "name": "rust_outer_python_inner::rust_outer"},
            {"id": 208, "name": "rust_outer_python_inner::main"},
        ]
        self.proxy.native_frames = reverse_native
        stacks = (
            ThreadStack(
                thread_id=77,
                frames=(Frame("python_inner", "/work/embedded.py", 2),),
            ),
        )

        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            return_value=stacks,
        ):
            response = self.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                self.context,
            )

        self.assertTrue(response.success)
        self.assertEqual(
            response.body,
            {"stackFrames": reverse_native, "totalFrames": 3},
        )

    def test_c_main_does_not_satisfy_the_rust_outer_boundary(self) -> None:
        reverse_native = [
            {"id": 201, "name": "rust_outer_python_inner::rust_callback"},
            {"id": 207, "name": "rust_outer_python_inner::rust_outer"},
            {"id": 208, "name": "main"},
        ]
        self.proxy.native_frames = reverse_native
        stacks = (
            ThreadStack(
                thread_id=77,
                frames=(
                    Frame("python_inner", "/work/embedded.py", 2),
                    Frame("python_outer", "/work/embedded.py", 6),
                ),
            ),
        )

        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            return_value=stacks,
        ):
            response = self.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                self.context,
            )

        self.assertTrue(response.success)
        self.assertEqual(
            response.body,
            {"stackFrames": reverse_native, "totalFrames": 3},
        )

    def test_missing_python_thread_returns_original_native_stack(self) -> None:
        stacks = (
            ThreadStack(
                thread_id=88,
                frames=(Frame("python_inner", "/work/app.py", 5),),
            ),
        )

        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            return_value=stacks,
        ):
            response = self.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                self.context,
            )

        self.assertTrue(response.success)
        self.assertEqual(
            response.body,
            {"stackFrames": self.native, "totalFrames": 3},
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

    def test_launch_preserves_codelldb_console_mode(self) -> None:
        forwarded = self.hooks.on_launch(
            {
                "arguments": {
                    "consoleMode": "evaluate",
                    "pyrustHelperTimeoutMs": 250,
                }
            },
            self.context,
        )

        self.assertEqual(
            forwarded["arguments"],
            {"consoleMode": "evaluate"},
        )

    def test_in_process_timeout_opens_session_circuit_and_bounds_workers(
        self,
    ) -> None:
        self.hooks.on_launch(
            {
                "arguments": {
                    "pyrustHelperTimeoutMs": 20,
                }
            },
            self.context,
        )
        entered = Event()
        release = Event()
        creation_lock = Lock()
        worker_creations = 0
        unwinder_workers: list[Thread] = []
        responses: list[object] = []

        def collect(pid: int) -> tuple[ThreadStack, ...]:
            entered.set()
            release.wait(2)
            return ()

        def make_unwinder_thread(*args: object, **kwargs: object) -> Thread:
            nonlocal worker_creations
            worker = Thread(*args, **kwargs)
            with creation_lock:
                worker_creations += 1
                unwinder_workers.append(worker)
            return worker

        def request_stack() -> None:
            responses.append(
                self.hooks.on_stack_trace(
                    {"arguments": {"threadId": 77}},
                    self.context,
                )
            )

        with (
            patch(
                "prototype.adapter.mixed_stack.read_python_stacks",
                side_effect=collect,
            ),
            patch(
                "prototype.adapter.mixed_stack.Thread",
                side_effect=make_unwinder_thread,
            ),
        ):
            first_request = Thread(target=request_stack)
            first_request.start()
            try:
                self.assertTrue(entered.wait(1))
                concurrent_requests = [
                    Thread(target=request_stack) for _ in range(3)
                ]
                for request in concurrent_requests:
                    request.start()
                for request in concurrent_requests:
                    request.join(1)
                first_request.join(1)

                self.assertFalse(first_request.is_alive())
                self.assertTrue(
                    all(not request.is_alive() for request in concurrent_requests)
                )
                self.assertEqual(worker_creations, 1)
                self.assertEqual(len(responses), 4)
                self.assertTrue(
                    all(
                        response.body
                        == {"stackFrames": self.native, "totalFrames": 3}
                        for response in responses
                    )
                )
                self.assertEqual(len(self.proxy.outputs), 1)
                self.assertIn("circuit opened", self.proxy.outputs[0].lower())

                self.state.on_continued()
                self.hooks.on_continued({"body": {}}, self.context)
                self.state.on_stopped()
                self.hooks.on_stopped({"body": {"threadId": 77}}, self.context)
                started = time.monotonic()
                later = self.hooks.on_stack_trace(
                    {"arguments": {"threadId": 77}},
                    self.context,
                )

                self.assertLess(time.monotonic() - started, 0.1)
                self.assertEqual(
                    later.body,
                    {"stackFrames": self.native, "totalFrames": 3},
                )
                self.assertEqual(worker_creations, 1)
                self.assertEqual(len(self.proxy.outputs), 1)
            finally:
                release.set()
                first_request.join(1)
                for worker in unwinder_workers:
                    worker.join(1)

    def test_current_native_id_precedes_old_synthetic_id(self) -> None:
        old_synthetic_id = self.state.synthetic_frames.allocate(
            77,
            ("python", "old"),
            {},
        )
        self.state.on_continued()
        self.hooks.on_continued({"body": {}}, self.context)
        self.state.on_stopped()
        self.hooks.on_stopped({"body": {"threadId": 77}}, self.context)
        self.proxy.native_frames = [
            {"id": old_synthetic_id, "name": "pyrust_native::rust_inner"},
            {"id": 102, "name": "pyrust_native::rust_outer"},
            {"id": 103, "name": "_PyEval_Vector"},
        ]
        stacks = (
            ThreadStack(
                thread_id=77,
                frames=(Frame("python_inner", "/work/app.py", 5),),
            ),
        )

        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            return_value=stacks,
        ):
            response = self.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                self.context,
            )

        self.assertTrue(response.success)
        assert response.body is not None
        frames = response.body["stackFrames"]
        self.assertEqual(frames[0]["id"], old_synthetic_id)
        self.assertNotEqual(frames[2]["id"], old_synthetic_id)
        self.assertEqual(
            self.state.synthetic_frames.classify(old_synthetic_id),
            "native",
        )

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

    def test_timed_out_stack_request_cannot_fallback_in_a_new_epoch(self) -> None:
        self.hooks.on_launch(
            {
                "arguments": {
                    "pyrustHelperTimeoutMs": 50,
                }
            },
            self.context,
        )
        entered = Event()
        release = Event()
        result: list[object] = []

        def collect(pid: int) -> tuple[ThreadStack, ...]:
            entered.set()
            release.wait(2)
            return ()

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
            self.hooks.on_continued({"body": {}}, self.context)
            self.state.on_stopped()
            self.hooks.on_stopped({"body": {"threadId": 77}}, self.context)
            worker.join(1)
            release.set()
            worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(result), 1)
        response = result[0]
        self.assertFalse(response.success)  # type: ignore[union-attr]
        self.assertIn("continued", response.message)  # type: ignore[union-attr]
        self.assertEqual(len(self.proxy.outputs), 1)
        self.assertIn("circuit opened", self.proxy.outputs[0].lower())

        later = self.hooks.on_stack_trace(
            {"arguments": {"threadId": 77}},
            self.context,
        )
        self.assertTrue(later.success)
        self.assertEqual(
            later.body,
            {"stackFrames": self.native, "totalFrames": 3},
        )
        self.assertEqual(len(self.proxy.outputs), 1)


if __name__ == "__main__":
    unittest.main()
