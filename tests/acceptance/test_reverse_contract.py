from __future__ import annotations

from threading import Event, Lock, Thread
import time
import unittest
from unittest.mock import patch

from prototype.adapter.mixed_stack import MixedStackHooks
from prototype.adapter.proxy import ProxyContext
from prototype.adapter.state import ProxySessionState
from prototype.python.pyrust_stack import Frame, LocalFrame, ThreadStack


class _ContextProxy:
    def __init__(self, native_frames: list[dict[str, object]]) -> None:
        self.native_frames = native_frames
        self.outputs: list[str] = []

    def request_downstream(
        self,
        command: str,
        arguments: dict[str, object] | None,
        *,
        timeout: float | None,
    ) -> dict[str, object]:
        del command, arguments, timeout
        return {
            "success": True,
            "body": {
                "stackFrames": self.native_frames,
                "totalFrames": len(self.native_frames),
            },
        }

    def send_upstream(self, message: dict[str, object]) -> None:
        self.outputs.append(str((message.get("body") or {}).get("output", "")))


class _ReverseContractFixture:
    def __init__(self) -> None:
        self.native = [
            {"id": 101, "name": "rust_outer_python_inner::rust_callback"},
            {"id": 102, "name": "_PyFunction_Vectorcall"},
            {"id": 103, "name": "rust_outer_python_inner::rust_outer"},
            {"id": 104, "name": "rust_outer_python_inner::main"},
        ]
        self.state = ProxySessionState()
        self.state.on_stopped()
        self.proxy = _ContextProxy(self.native)
        self.context = ProxyContext(self.proxy, self.state)  # type: ignore[arg-type]
        self.hooks = MixedStackHooks()
        self.hooks.on_stopped({"body": {"threadId": 77}}, self.context)


class ReverseStabilizationContractTests(unittest.TestCase):
    def test_ac_bf_01_single_worker_for_concurrent_blocked_requests(self) -> None:
        fixture = _ReverseContractFixture()
        fixture.hooks.on_launch(
            {"arguments": {"pyrustHelperTimeoutMs": 20}},
            fixture.context,
        )
        entered = Event()
        release = Event()
        worker_creations = 0
        worker_lock = Lock()
        workers: list[Thread] = []
        responses: list[object] = []

        def blocked_reader(pid: int) -> tuple[ThreadStack, ...]:
            del pid
            entered.set()
            release.wait(2)
            return ()

        def make_worker(*args: object, **kwargs: object) -> Thread:
            nonlocal worker_creations
            worker = Thread(*args, **kwargs)
            with worker_lock:
                worker_creations += 1
                workers.append(worker)
            return worker

        def request_stack() -> None:
            responses.append(
                fixture.hooks.on_stack_trace(
                    {"arguments": {"threadId": 77}},
                    fixture.context,
                )
            )

        try:
            with (
                patch(
                    "prototype.adapter.mixed_stack.read_python_stacks",
                    side_effect=blocked_reader,
                ),
                patch(
                    "prototype.adapter.mixed_stack.Thread",
                    side_effect=make_worker,
                ),
            ):
                first = Thread(target=request_stack)
                first.start()
                self.assertTrue(entered.wait(1))
                peers = [Thread(target=request_stack) for _ in range(3)]
                for peer in peers:
                    peer.start()
                for peer in peers:
                    peer.join(1)
                first.join(1)

            self.assertEqual(worker_creations, 1)
            self.assertEqual(len(responses), 4)
            self.assertTrue(
                all(
                    response.body
                    == {"stackFrames": fixture.native, "totalFrames": 4}
                    for response in responses
                )
            )
        finally:
            release.set()
            for worker in workers:
                worker.join(1)

    def test_ac_bf_02_timeout_opens_circuit_and_later_requests_are_immediate(
        self,
    ) -> None:
        fixture = _ReverseContractFixture()
        fixture.hooks.on_launch(
            {"arguments": {"pyrustHelperTimeoutMs": 20}},
            fixture.context,
        )
        release = Event()

        def blocked_reader(pid: int) -> tuple[ThreadStack, ...]:
            del pid
            release.wait(2)
            return ()

        try:
            with patch(
                "prototype.adapter.mixed_stack.read_python_stacks",
                side_effect=blocked_reader,
            ):
                started = time.monotonic()
                first = fixture.hooks.on_stack_trace(
                    {"arguments": {"threadId": 77}},
                    fixture.context,
                )
                first_elapsed = time.monotonic() - started
                started = time.monotonic()
                second = fixture.hooks.on_stack_trace(
                    {"arguments": {"threadId": 77}},
                    fixture.context,
                )
                second_elapsed = time.monotonic() - started

            self.assertLess(first_elapsed, 0.5)
            self.assertLess(second_elapsed, 0.1)
            self.assertEqual(first.body, second.body)
            self.assertEqual(len(fixture.proxy.outputs), 1)
        finally:
            release.set()

    def test_ac_bf_03_timeout_diagnostic_is_emitted_once_across_epochs(self) -> None:
        fixture = _ReverseContractFixture()
        fixture.hooks.on_launch(
            {"arguments": {"pyrustHelperTimeoutMs": 20}},
            fixture.context,
        )

        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            side_effect=lambda pid: time.sleep(1),
        ):
            fixture.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                fixture.context,
            )
            fixture.state.on_continued()
            fixture.hooks.on_continued({"body": {}}, fixture.context)
            fixture.state.on_stopped()
            fixture.hooks.on_stopped({"body": {"threadId": 77}}, fixture.context)
            fixture.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                fixture.context,
            )

        self.assertEqual(len(fixture.proxy.outputs), 1)
        self.assertIn("circuit opened", fixture.proxy.outputs[0].lower())

    def test_ac_bf_05_late_result_cannot_allocate_into_new_epoch(self) -> None:
        fixture = _ReverseContractFixture()
        entered = Event()
        release = Event()
        result: list[object] = []

        def delayed_reader(pid: int) -> tuple[ThreadStack, ...]:
            del pid
            entered.set()
            release.wait(2)
            return (
                ThreadStack(
                    thread_id=77,
                    frames=(Frame("python_inner", "/fixture/embedded.py", 1),),
                ),
            )

        try:
            with patch(
                "prototype.adapter.mixed_stack.read_python_stacks",
                side_effect=delayed_reader,
            ):
                worker = Thread(
                    target=lambda: result.append(
                        fixture.hooks.on_stack_trace(
                            {"arguments": {"threadId": 77}},
                            fixture.context,
                        )
                    )
                )
                worker.start()
                self.assertTrue(entered.wait(1))
                fixture.state.on_continued()
                fixture.hooks.on_continued({"body": {}}, fixture.context)
                fixture.state.on_stopped()
                fixture.hooks.on_stopped({"body": {"threadId": 77}}, fixture.context)
                release.set()
                worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(result), 1)
            self.assertFalse(result[0].success)  # type: ignore[union-attr]
            self.assertIn("continued", result[0].message)  # type: ignore[union-attr]
            self.assertEqual(fixture.state.synthetic_frames.epoch, 2)

            old_synthetic_id = fixture.state.synthetic_frames.allocate(
                77,
                ("python", "old"),
                {"name": "python_inner"},
            )
            fixture.proxy.native_frames[0]["id"] = old_synthetic_id
            with patch(
                "prototype.adapter.mixed_stack.read_python_stacks",
                return_value=(
                    ThreadStack(
                        thread_id=77,
                        frames=(
                            Frame("python_inner", "/fixture/embedded.py", 1),
                            Frame("python_outer", "/fixture/embedded.py", 2),
                        ),
                    ),
                ),
            ):
                response = fixture.hooks.on_stack_trace(
                    {"arguments": {"threadId": 77}},
                    fixture.context,
                )

            self.assertTrue(response.success)
            assert response.body is not None
            merged_frames = response.body["stackFrames"]
            self.assertEqual(merged_frames[0]["id"], old_synthetic_id)
            self.assertEqual(
                fixture.state.synthetic_frames.classify(old_synthetic_id),
                "native",
            )
            self.assertEqual(
                [frame["name"] for frame in merged_frames[:4]],
                [
                    "rust_outer_python_inner::rust_callback",
                    "python_inner",
                    "python_outer",
                    "_PyFunction_Vectorcall",
                ],
            )
            self.assertNotEqual(merged_frames[1]["id"], old_synthetic_id)
            self.assertNotEqual(merged_frames[2]["id"], old_synthetic_id)
        finally:
            release.set()


class ReverseShapeContractTests(unittest.TestCase):
    def test_ac_rp_02_required_reverse_order_and_post_merge_paging(self) -> None:
        fixture = _ReverseContractFixture()
        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            return_value=(
                ThreadStack(
                    thread_id=77,
                    frames=(
                        Frame("python_inner", "/fixture/embedded.py", 1),
                        Frame("python_outer", "/fixture/embedded.py", 2),
                        Frame("<module>", "/fixture/embedded.py", 3),
                    ),
                ),
            ),
        ):
            response = fixture.hooks.on_stack_trace(
                {
                    "arguments": {
                        "threadId": 77,
                        "startFrame": 1,
                        "levels": 3,
                    }
                },
                fixture.context,
            )

        self.assertTrue(response.success)
        assert response.body is not None
        self.assertEqual(response.body["totalFrames"], 7)
        self.assertEqual(
            [frame["name"] for frame in response.body["stackFrames"]],
            ["python_inner", "python_outer", "<module>"],
        )

    def test_ac_rp_05_synthetic_frames_expose_snapshot_locals(self) -> None:
        fixture = _ReverseContractFixture()
        stacks = (
            ThreadStack(
                thread_id=77,
                frames=(
                    Frame("python_inner", "/fixture/embedded.py", 4),
                    Frame("python_outer", "/fixture/embedded.py", 9),
                ),
            ),
        )
        snapshots = (
            LocalFrame(
                "python_inner",
                "/fixture/embedded.py",
                {"value": 20, "label": "rust-to-python"},
            ),
            LocalFrame("python_outer", "/fixture/embedded.py", {"value": 21}),
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
            response = fixture.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                fixture.context,
            )

        assert response.body is not None
        synthetic_id = response.body["stackFrames"][1]["id"]
        self.assertEqual(
            fixture.state.synthetic_frames.classify(synthetic_id),
            "current",
        )
        scopes = fixture.hooks.on_scopes(
            {"arguments": {"frameId": synthetic_id}},
            fixture.context,
        )
        self.assertEqual(
            scopes.body,
            {
                "scopes": [
                    {
                        "name": "Python Locals",
                        "presentationHint": "locals",
                        "variablesReference": synthetic_id,
                        "expensive": False,
                    }
                ]
            },
        )
        variables = fixture.hooks.on_variables(
            {"arguments": {"variablesReference": synthetic_id}},
            fixture.context,
        )
        assert variables.body is not None
        self.assertIn(
            {
                "name": "value",
                "value": "20",
                "type": "int",
                "variablesReference": 0,
                "evaluateName": "value",
            },
            variables.body["variables"],
        )
        evaluation = fixture.hooks.on_evaluate(
            {
                "arguments": {
                    "frameId": synthetic_id,
                    "expression": "value + 1",
                }
            },
            fixture.context,
        )
        self.assertEqual(evaluation.body, {"result": "21", "type": "int", "variablesReference": 0})

    def test_ac_rp_06_unknown_boundaries_preserve_native_stack(self) -> None:
        fixture = _ReverseContractFixture()
        fixture.native = [
            {"id": 101, "name": "rust_outer_python_inner::rust_callback"},
            {"id": 102, "name": "application::native_helper"},
            {"id": 103, "name": "rust_outer_python_inner::main"},
        ]
        fixture.proxy.native_frames = fixture.native
        with patch(
            "prototype.adapter.mixed_stack.read_python_stacks",
            return_value=(
                ThreadStack(
                    thread_id=77,
                    frames=(
                        Frame("python_inner", "/fixture/embedded.py", 1),
                        Frame("python_outer", "/fixture/embedded.py", 2),
                    ),
                ),
            ),
        ):
            response = fixture.hooks.on_stack_trace(
                {"arguments": {"threadId": 77}},
                fixture.context,
            )

        self.assertEqual(response.body, {"stackFrames": fixture.native, "totalFrames": 3})


if __name__ == "__main__":
    unittest.main()
