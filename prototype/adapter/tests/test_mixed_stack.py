from __future__ import annotations

import unittest
from unittest.mock import patch
from threading import Event, Lock, Thread
import time

from prototype.adapter.mixed_stack import MixedStackHooks, _NativeLeaseStep
from prototype.adapter.proxy import ProxyContext
from prototype.adapter.state import ProxySessionState
from prototype.python.pyrust_stack import (
    Frame,
    LocalFrame,
    StackReadError,
    ThreadStack,
)


class _ContextProxy:
    def __init__(self, native_frames: list[dict[str, object]]) -> None:
        self.native_frames = native_frames
        self.outputs: list[str] = []
        self.stack_arguments: list[dict[str, object]] = []
        self.downstream_requests: list[tuple[str, dict[str, object]]] = []

    def request_downstream(
        self,
        command: str,
        arguments: dict[str, object] | None,
        *,
        timeout: float | None,
    ) -> dict[str, object]:
        if command == "threads":
            return {
                "success": True,
                "body": {
                    "threads": [
                        {"id": 77, "name": "rust-child-A"},
                        {"id": 78, "name": "rust-child-B"},
                    ]
                },
            }
        assert arguments is not None
        self.downstream_requests.append((command, dict(arguments)))
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

    def test_process_tree_refreshes_native_threads_without_call_stack_request(self) -> None:
        self.state.register_process(100, display_name="Python process")
        self.state.on_stopped(
            {"body": {"systemProcessId": 100, "threadId": 77}}
        )

        response = self.hooks.on_process_tree({}, self.context)

        self.assertTrue(response.success)
        assert response.body is not None
        process = response.body["processes"][0]
        self.assertEqual(
            [thread["threadId"] for thread in process["threads"]],
            [77, 78],
        )
        self.assertEqual(
            [thread["name"] for thread in process["threads"]],
            ["rust-child-A", "rust-child-B"],
        )

    def test_native_lease_step_forwards_the_selected_operation(self) -> None:
        for command in ("next", "stepIn", "stepOut"):
            with self.subTest(command=command):
                self.hooks._continue_native_lease_step(
                    _NativeLeaseStep(
                        process_id=100,
                        thread_id=77,
                        command=command,
                        arguments={"granularity": "line", "singleThread": True},
                    ),
                    self.context,
                )
                self.assertEqual(
                    self.proxy.downstream_requests[-1],
                    (
                        command,
                        {
                            "granularity": "line",
                            "singleThread": True,
                            "threadId": 77,
                        },
                    ),
                )

    def test_successful_python_step_in_clears_future_stop_suppression(self) -> None:
        self.hooks._python_step_in_processes.add(100)
        self.hooks._python_step_in_suppress_python.add(100)
        self.hooks._python_handoffs.add((100, 77))

        event = self.hooks.on_stopped(
            {"body": {"systemProcessId": 100, "threadId": 77}},
            self.context,
        )

        assert event is not None
        self.assertEqual(event["body"]["reason"], "step")
        self.assertNotIn(100, self.hooks._python_step_in_processes)
        self.assertNotIn(100, self.hooks._python_step_in_suppress_python)
        self.assertFalse(self.hooks._python_handoffs)

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

    def test_native_only_rust_child_skips_python_unwinder_and_diagnostic(
        self,
    ) -> None:
        native_only = [
            {"id": 101, "name": "pyrust_native::rust_inner"},
            {
                "id": 102,
                "name": (
                    "pyrust_native::rust_outer_with_rust_threads::"
                    "{closure#0}::{closure#0}::{closure#0}"
                ),
            },
        ]
        self.proxy.native_frames = native_only

        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            side_effect=AssertionError("native-only stack must not unwind Python"),
        ):
            response = self.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                self.context,
            )

        self.assertTrue(response.success)
        self.assertEqual(
            response.body,
            {"stackFrames": native_only, "totalFrames": 2},
        )
        self.assertEqual(self.proxy.outputs, [])

    def test_single_rust_inner_frame_is_normal_native_fallback(self) -> None:
        native_only = [{"id": 101, "name": "pyrust_native::rust_inner"}]
        self.proxy.native_frames = native_only

        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            side_effect=AssertionError("native-only stack must not unwind Python"),
        ):
            response = self.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                self.context,
            )

        self.assertTrue(response.success)
        self.assertEqual(
            response.body,
            {"stackFrames": native_only, "totalFrames": 1},
        )
        self.assertEqual(self.proxy.outputs, [])

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

    def test_rust_async_poll_frame_satisfies_reverse_boundary(self) -> None:
        native = [
            {"id": 201, "name": "rust_outer_python_async::rust_callback"},
            {"id": 202, "name": "_PyFunction_Vectorcall"},
            {
                "id": 203,
                "name": "rust_outer_python_async::rust_outer::{closure#0}",
            },
            {
                "id": 204,
                "name": "rust_outer_python_async::async_task::{closure#0}",
            },
            {"id": 205, "name": "rust_outer_python_async::main"},
        ]
        python = [
            {"name": "python_inner", "path": "/work/async_embedded.py", "line": 8},
            {"name": "python_outer", "path": "/work/async_embedded.py", "line": 12},
            {
                "name": "_run",
                "path": "/usr/lib/python3.14/asyncio/events.py",
                "line": 84,
            },
        ]

        self.assertEqual(MixedStackHooks._python_boundary_index(native), 1)

    def test_rust_outer_pages_after_crossing_both_language_boundaries(
        self,
    ) -> None:
        reverse_native = [
            {"id": 201, "name": "rust_outer_python_inner::rust_callback"},
            {"id": 202, "name": "_PyFunction_Vectorcall"},
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
        self.assertEqual(response.body["totalFrames"], 6)
        self.assertEqual(
            [frame["name"] for frame in response.body["stackFrames"]],
            [
                "rust_outer_python_inner::rust_callback",
                "python_inner",
                "python_outer",
                "_PyFunction_Vectorcall",
            ],
        )
        self.assertEqual(self.proxy.stack_arguments, [{"threadId": 77}])

    def test_rust_outer_allows_an_embedded_module_frame(self) -> None:
        reverse_native = [
            {"id": 201, "name": "rust_outer_python_inner::rust_callback"},
            {"id": 202, "name": "_PyFunction_Vectorcall"},
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
                "_PyFunction_Vectorcall",
                "rust_outer_python_inner::rust_outer",
                "rust_outer_python_inner::main",
            ],
        )

    def test_unrelated_function_names_merge_at_structural_python_bridge(
        self,
    ) -> None:
        unknown_native = [
            {"id": 201, "name": "application::serve_request"},
            {"id": 202, "name": "_PyFunction_Vectorcall"},
            {"id": 208, "name": "application::run_server"},
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
            [frame["name"] for frame in response.body["stackFrames"]],
            [
                "application::serve_request",
                "python_inner",
                "python_outer",
                "_PyFunction_Vectorcall",
                "application::run_server",
            ],
        )
        self.assertEqual(self.proxy.outputs, [])

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

        self.assertEqual(forwarded["arguments"]["consoleMode"], "evaluate")
        self.assertEqual(
            forwarded["arguments"]["env"]["PYRUST_DEBUGPY_ENABLE"],
            "1",
        )

    def test_python_locals_scopes_and_safe_evaluation(self) -> None:
        stacks = (
            ThreadStack(
                thread_id=77,
                frames=(
                    Frame("python_inner", "/work/app.py", 6),
                    Frame("python_outer", "/work/app.py", 11),
                ),
            ),
        )
        snapshots = (
            LocalFrame(
                "python_inner",
                "/work/app.py",
                {"value": 20, "label": "python-to-rust"},
            ),
            LocalFrame("python_outer", "/work/app.py", {"value": 20}),
        )
        with (
            patch(
                "prototype.adapter.mixed_stack.read_python_stacks",
                return_value=stacks,
            ),
            patch(
                "prototype.adapter.mixed_stack.read_python_locals",
                return_value=snapshots,
            ),
        ):
            stack_response = self.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                self.context,
            )

        assert stack_response.body is not None
        python_id = stack_response.body["stackFrames"][2]["id"]
        scopes = self.hooks.on_scopes(
            {"arguments": {"frameId": python_id}},
            self.context,
        )
        self.assertEqual(
            scopes.body,
            {
                "scopes": [
                    {
                        "name": "Python Locals",
                        "presentationHint": "locals",
                        "variablesReference": python_id,
                        "expensive": False,
                    }
                ]
            },
        )
        variables = self.hooks.on_variables(
            {"arguments": {"variablesReference": python_id}},
            self.context,
        )
        self.assertEqual(
            variables.body,
            {
                "variables": [
                    {
                        "name": "label",
                        "value": "'python-to-rust'",
                        "type": "str",
                        "variablesReference": 0,
                        "evaluateName": "label",
                    },
                    {
                        "name": "value",
                        "value": "20",
                        "type": "int",
                        "variablesReference": 0,
                        "evaluateName": "value",
                    },
                ]
            },
        )
        evaluated = self.hooks.on_evaluate(
            {
                "arguments": {
                    "frameId": python_id,
                    "expression": "value + 1",
                }
            },
            self.context,
        )
        self.assertEqual(
            evaluated.body,
            {"result": "21", "type": "int", "variablesReference": 0},
        )
        frameless_console = self.hooks.on_evaluate(
            {"arguments": {"expression": "label"}},
            self.context,
        )
        self.assertEqual(
            frameless_console.body,
            {
                "result": "'python-to-rust'",
                "type": "str",
                "variablesReference": 0,
            },
        )
        self.assertIsNone(
            self.hooks.on_scopes(
                {"arguments": {"frameId": 101}},
                self.context,
            )
        )
        self.assertIsNone(
            self.hooks.on_evaluate(
                {"arguments": {"expression": "native_value"}},
                self.context,
            )
        )
        boolean_short_circuit = self.hooks.on_evaluate(
            {
                "arguments": {
                    "frameId": python_id,
                    "expression": "label or __import__('os')",
                }
            },
            self.context,
        )
        self.assertEqual(
            boolean_short_circuit.body,
            {
                "result": "'python-to-rust'",
                "type": "str",
                "variablesReference": 0,
            },
        )
        unsupported = self.hooks.on_evaluate(
            {
                "arguments": {
                    "frameId": python_id,
                    "expression": "__import__('os')",
                }
            },
            self.context,
        )
        self.assertFalse(unsupported.success)
        self.assertIn("Call", unsupported.message)

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
