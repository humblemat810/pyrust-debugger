from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
from typing import Any, Callable, Mapping, Sequence
import unittest
from unittest.mock import patch

from prototype.adapter.python_manager import (
    PythonProcessManager,
    read_debugpy_registry,
)
from prototype.adapter.state import ProxySessionState


Message = dict[str, Any]


class FakeDebugpyTransport:
    def __init__(self, event_handler: Callable[[Message], None]) -> None:
        self.event_handler = event_handler
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def start(
        self,
        *,
        host: str,
        port: int,
        breakpoints: Sequence[Mapping[str, Any]],
    ) -> None:
        self.calls.append(
            (
                "start",
                {
                    "host": host,
                    "port": port,
                    "breakpoints": [dict(item) for item in breakpoints],
                },
            )
        )

    def request(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> Message:
        payload = dict(arguments or {})
        self.calls.append((command, payload))
        if command == "threads":
            return {
                "success": True,
                "body": {"threads": [{"id": 701, "name": "worker-A"}]},
            }
        if command == "stackTrace":
            return {
                "success": True,
                "body": {
                    "stackFrames": [
                        {
                            "id": 11,
                            "name": "python_worker",
                            "line": 24,
                            "column": 1,
                            "source": {"path": "/workspace/worker.py"},
                        }
                    ]
                },
            }
        if command == "scopes":
            return {
                "success": True,
                "body": {
                    "scopes": [
                        {
                            "name": "Locals",
                            "variablesReference": 31,
                            "expensive": False,
                        }
                    ]
                },
            }
        if command == "variables":
            return {
                "success": True,
                "body": {
                    "variables": [
                        {
                            "name": "value",
                            "value": "20",
                            "variablesReference": 0,
                        }
                    ]
                },
            }
        if command in {"setVariable", "setExpression"}:
            return {
                "success": True,
                "body": {
                    "value": arguments.get("value", ""),
                    "type": "int",
                    "variablesReference": 0,
                },
            }
        if command == "evaluate":
            if arguments.get("expression") == "__import__('_thread').get_native_id()":
                return {
                    "success": True,
                    "body": {
                        "result": "1701",
                        "type": "int",
                        "variablesReference": 0,
                    },
                }
            return {
                "success": True,
                "body": {
                    "result": "(3, 14)",
                    "type": "tuple",
                    "variablesReference": 0,
                },
            }
        if command == "setBreakpoints":
            return {
                "success": True,
                "body": {"breakpoints": [{"verified": True}]},
            }
        if command in {"continue", "next", "pause", "stepIn", "stepOut"}:
            return {"success": True, "body": {}}
        raise AssertionError(f"unexpected fake request {command!r}")

    def notify(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        self.calls.append((command, dict(arguments or {})))

    def request_async(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        on_error: Callable[[Exception], None],
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        del on_error
        self.calls.append((command, dict(arguments or {})))
        if on_complete is not None:
            on_complete()

    def close(self) -> None:
        self.closed = True


class FailingDebugpyTransport(FakeDebugpyTransport):
    def start(
        self,
        *,
        host: str,
        port: int,
        breakpoints: Sequence[Mapping[str, Any]],
    ) -> None:
        del host, port, breakpoints
        raise RuntimeError("fixture attach failure")


class PythonProcessManagerTests(unittest.TestCase):
    def test_registry_removes_dead_process_records(self) -> None:
        with TemporaryDirectory() as directory:
            registry = Path(directory)
            record = registry / "debugpy-999999999.json"
            failure = registry / "debugpy-999999999.failed"
            record.write_text(
                '{"pid":999999999,"parentPid":1,'
                '"host":"127.0.0.1","port":5678}',
                encoding="utf-8",
            )
            failure.write_text("old failure", encoding="utf-8")

            self.assertEqual(read_debugpy_registry(registry), ())
            self.assertFalse(record.exists())
            self.assertFalse(failure.exists())

    def test_user_breakpoint_is_forwarded_without_hidden_breakpoints(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "app.py"
            source.write_text(
                "import pyrust_native\n"
                "value = pyrust_native.rust_outer(20)\n"
                "print(value)\n",
                encoding="utf-8",
            )
            manager = PythonProcessManager(
                registry_path=root / "registry",
                state=ProxySessionState(),
                emit_event=lambda _event, _body: None,
            )
            manager.add_breakpoints(
                {
                    "source": {"path": str(source)},
                    "breakpoints": [{"line": 2}],
                }
            )

            record = next(
                item
                for item in manager._all_breakpoints()
                if item["source"]["path"] == str(source)
            )
            self.assertEqual(record["breakpoints"], [{"line": 2}])
            manager.close()

    def test_python_routes_are_virtualized_and_release_ownership_on_continue(
        self,
    ) -> None:
        state = ProxySessionState()
        emitted: list[tuple[str, Mapping[str, Any]]] = []
        created: list[FakeDebugpyTransport] = []
        with TemporaryDirectory() as directory:
            registry = Path(directory)

            def factory(
                event_handler: Callable[[Message], None],
            ) -> FakeDebugpyTransport:
                transport = FakeDebugpyTransport(event_handler)
                created.append(transport)
                return transport

            manager = PythonProcessManager(
                registry_path=registry,
                state=state,
                emit_event=lambda event, body: emitted.append((event, body)),
                transport_factory=factory,
            )
            manager.add_breakpoints(
                {
                    "source": {"path": "/workspace/worker.py"},
                    "breakpoints": [{"line": 24}],
                }
            )
            manager._start_process(
                {
                    "pid": 700,
                    "parentPid": 600,
                    "host": "127.0.0.1",
                    "port": 5678,
                }
            )
            created[0].event_handler(
                {
                    "type": "event",
                    "event": "stopped",
                    "body": {"threadId": 701, "reason": "breakpoint"},
                }
            )

            threads = manager.threads()
            self.assertEqual(len(threads), 1)
            virtual_thread_id = threads[0]["id"]
            self.assertGreaterEqual(virtual_thread_id, 1_600_000_000)
            self.assertEqual(state.process_id_for_thread(virtual_thread_id), 700)
            self.assertEqual(state.coordinator.execution_owner(700), "python")

            stack = manager.stack_trace(virtual_thread_id, {})
            frame_id = stack["body"]["stackFrames"][0]["id"]
            self.assertGreaterEqual(frame_id, 1_700_000_000)
            self.assertEqual(
                manager.recent_frames(700),
                [
                    {
                        "name": "python_worker",
                        "path": "/workspace/worker.py",
                        "line": 24,
                    }
                ],
            )
            self.assertEqual(
                manager.native_identity_for_thread(virtual_thread_id),
                (700, 1701),
            )
            self.assertEqual(
                created[0].calls[-1],
                (
                    "evaluate",
                    {
                        "expression": "__import__('_thread').get_native_id()",
                        "frameId": 11,
                        "context": "watch",
                    },
                ),
            )
            self.assertEqual(
                manager.scopes(frame_id)["body"]["scopes"][0]["name"],
                "Locals",
            )
            reference = manager.scopes(frame_id)["body"]["scopes"][0][
                "variablesReference"
            ]
            self.assertGreaterEqual(reference, 1_800_000_000)
            self.assertEqual(
                manager.variables(reference)["body"]["variables"][0]["name"],
                "value",
            )
            self.assertEqual(
                manager.set_variable(
                    reference,
                    {"name": "value", "value": "41"},
                )["body"]["value"],
                "41",
            )
            self.assertEqual(
                created[0].calls[-1],
                (
                    "setVariable",
                    {
                        "variablesReference": 31,
                        "name": "value",
                        "value": "41",
                    },
                ),
            )
            self.assertEqual(
                manager.set_expression(
                    frame_id,
                    {"expression": "value", "value": "42"},
                )["body"]["value"],
                "42",
            )
            self.assertEqual(
                created[0].calls[-1],
                (
                    "setExpression",
                    {
                        "expression": "value",
                        "value": "42",
                        "frameId": 11,
                    },
                ),
            )
            self.assertEqual(
                manager.evaluate(frame_id, {"expression": "__import__('sys')"})[
                    "body"
                ]["result"],
                "(3, 14)",
            )

            created[0].event_handler(
                {
                    "type": "event",
                    "event": "stopped",
                    "body": {
                        "threadId": 702,
                        "reason": "pause",
                        "allThreadsStopped": True,
                    },
                }
            )
            manager.pause_thread(virtual_thread_id)
            self.assertEqual(
                created[0].calls[-1],
                ("pause", {"threadId": 701}),
            )
            with patch(
                "prototype.adapter.python_manager.queue_remote_debug_script",
                side_effect=lambda *_args: (
                    registry / "handoff-entered-700"
                ).touch(),
            ) as remote_exec:
                manager.arm_targeted_handoff(
                    700,
                    native_thread_id=700,
                    target_name="python_worker",
                    target_path="/workspace/worker.py",
                )
            remote_exec.assert_called_once_with(
                700,
                700,
                registry / "pyrust-debugpy-handoff.py",
            )
            deadline = time.monotonic() + 1
            while (
                created[0].calls[-1] != ("pause", {"threadId": "*"})
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)
            self.assertEqual(
                created[0].calls[-1],
                ("pause", {"threadId": "*"}),
            )
            manager.step_thread(
                "stepIn",
                virtual_thread_id,
                {"granularity": "line", "singleThread": True},
            )
            self.assertEqual(
                created[0].calls[-1],
                (
                    "stepIn",
                    {
                        "granularity": "line",
                        "singleThread": True,
                        "threadId": 701,
                    },
                ),
            )
            created[0].event_handler(
                {
                    "type": "event",
                    "event": "continued",
                    "body": {
                        "threadId": 701,
                        "allThreadsContinued": False,
                    },
                }
            )
            self.assertEqual(state.coordinator.execution_owner(700), "python")
            manager.continue_thread(virtual_thread_id)
            self.assertEqual(
                created[0].calls[-1],
                (
                    "continue",
                    {"threadId": 701, "singleThread": False},
                ),
            )
            created[0].event_handler(
                {
                    "type": "event",
                    "event": "continued",
                    "body": {
                        "threadId": 701,
                        "allThreadsContinued": True,
                    },
                }
            )
            self.assertIsNone(state.coordinator.execution_owner(700))
            self.assertIsNone(manager.native_frame_route(frame_id))
            self.assertEqual(
                manager.recent_frames(700)[0]["name"],
                "python_worker",
            )
            self.assertIn(
                (
                    "continued",
                    {
                        "threadId": virtual_thread_id,
                        "allThreadsContinued": True,
                        "systemProcessId": 700,
                    },
                ),
                emitted,
            )
            manager.close()
            self.assertTrue(created[0].closed)

    def test_breakpoint_added_during_attach_reaches_new_session(self) -> None:
        state = ProxySessionState()
        attach_started = Event()
        finish_attach = Event()
        created: list[FakeDebugpyTransport] = []

        class BlockingTransport(FakeDebugpyTransport):
            def start(
                self,
                *,
                host: str,
                port: int,
                breakpoints: Sequence[Mapping[str, Any]],
            ) -> None:
                super().start(host=host, port=port, breakpoints=breakpoints)
                attach_started.set()
                finish_attach.wait(2)

        with TemporaryDirectory() as directory:
            registry = Path(directory)

            def factory(
                event_handler: Callable[[Message], None],
            ) -> FakeDebugpyTransport:
                transport = BlockingTransport(event_handler)
                created.append(transport)
                return transport

            manager = PythonProcessManager(
                registry_path=registry,
                state=state,
                emit_event=lambda _event, _body: None,
                transport_factory=factory,
            )
            worker = Thread(
                target=manager._start_process,
                args=(
                    {
                        "pid": 700,
                        "parentPid": 600,
                        "host": "127.0.0.1",
                        "port": 5678,
                    },
                ),
            )
            worker.start()
            self.assertTrue(attach_started.wait(1))
            manager.add_breakpoints(
                {
                    "source": {"path": "/workspace/worker.py"},
                    "breakpoints": [{"line": 24}],
                }
            )
            finish_attach.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertIn(
                (
                    "setBreakpoints",
                    {
                        "source": {"path": "/workspace/worker.py"},
                        "breakpoints": [{"line": 24}],
                        "sourceModified": False,
                    },
                ),
                created[0].calls,
            )
            manager.close()

    def test_breakpoints_update_an_attached_debugpy_process(self) -> None:
        state = ProxySessionState()
        created: list[FakeDebugpyTransport] = []
        with TemporaryDirectory() as directory:
            registry = Path(directory)

            def factory(
                event_handler: Callable[[Message], None],
            ) -> FakeDebugpyTransport:
                transport = FakeDebugpyTransport(event_handler)
                created.append(transport)
                return transport

            manager = PythonProcessManager(
                registry_path=registry,
                state=state,
                emit_event=lambda _event, _body: None,
                transport_factory=factory,
            )
            manager._start_process(
                {
                    "pid": 700,
                    "parentPid": 600,
                    "host": "127.0.0.1",
                    "port": 5678,
                }
            )
            manager.add_breakpoints(
                {
                    "source": {"path": "/workspace/worker.py"},
                    "breakpoints": [{"line": 24}],
                }
            )
            self.assertIn(
                (
                    "setBreakpoints",
                    {
                        "source": {"path": "/workspace/worker.py"},
                        "breakpoints": [{"line": 24}],
                        "sourceModified": False,
                    },
                ),
                created[0].calls,
            )
            manager.close()

    def test_attach_failure_is_marked_and_not_retried(self) -> None:
        state = ProxySessionState()
        emitted: list[tuple[str, Mapping[str, Any]]] = []
        created: list[FailingDebugpyTransport] = []
        with TemporaryDirectory() as directory:
            registry = Path(directory)

            def factory(
                event_handler: Callable[[Message], None],
            ) -> FailingDebugpyTransport:
                transport = FailingDebugpyTransport(event_handler)
                created.append(transport)
                return transport

            manager = PythonProcessManager(
                registry_path=registry,
                state=state,
                emit_event=lambda event, body: emitted.append((event, body)),
                transport_factory=factory,
            )
            record = {
                "pid": 700,
                "parentPid": 600,
                "host": "127.0.0.1",
                "port": 5678,
            }

            manager._start_process(record)
            manager._start_process(record)

            self.assertEqual(len(created), 1)
            self.assertTrue((registry / "debugpy-700.failed").is_file())
            self.assertTrue(
                any(
                    "attach failed" in str(body.get("output", ""))
                    for event, body in emitted
                    if event == "output"
                )
            )
            manager.close()


if __name__ == "__main__":
    unittest.main()
