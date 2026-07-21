from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping, Sequence
import unittest

from prototype.adapter.python_manager import PythonProcessManager
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
        if command == "evaluate":
            return {
                "success": True,
                "body": {
                    "result": "(3, 14)",
                    "type": "tuple",
                    "variablesReference": 0,
                },
            }
        raise AssertionError(f"unexpected fake request {command!r}")

    def notify(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        self.calls.append((command, dict(arguments or {})))

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
                manager.evaluate(frame_id, {"expression": "__import__('sys')"})[
                    "body"
                ]["result"],
                "(3, 14)",
            )

            manager.continue_thread(virtual_thread_id)
            self.assertIsNone(state.coordinator.execution_owner(700))
            self.assertIsNone(manager.native_frame_route(frame_id))
            self.assertEqual(
                manager.recent_frames(700)[0]["name"],
                "python_worker",
            )
            self.assertEqual(
                created[0].calls[-1],
                (
                    "continue",
                    {"threadId": 701, "singleThread": False},
                ),
            )
            self.assertIn(
                (
                    "continued",
                    {
                        "threadId": virtual_thread_id,
                        "allThreadsContinued": True,
                    },
                ),
                emitted,
            )
            manager.close()
            self.assertTrue(created[0].closed)

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
