from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping, Sequence
import unittest

from prototype.adapter.process_manager import ProcessManager
from prototype.adapter.state import ProxySessionState


Message = dict[str, Any]


class FakeChildTransport:
    def __init__(self, event_handler: Callable[[Message], None]) -> None:
        self.event_handler = event_handler
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def start(
        self,
        *,
        process_id: int,
        breakpoints: Sequence[Mapping[str, Any]],
    ) -> None:
        self.calls.append(
            (
                "start",
                {
                    "processId": process_id,
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
            return {"success": True, "body": {"threads": [{"id": 701}]}}
        if command == "stackTrace":
            return {
                "success": True,
                "body": {
                    "stackFrames": [
                        {
                            "id": 11,
                            "name": "pyrust_native::rust_inner",
                            "line": 6,
                            "column": 1,
                            "variablesReference": 0,
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
                            "name": "Local",
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
                    "result": "21",
                    "type": "i64",
                    "variablesReference": 0,
                },
            }
        if command in {
            "continue",
            "next",
            "stepIn",
            "stepOut",
            "pause",
            "setVariable",
            "setExpression",
        }:
            if command in {"setVariable", "setExpression"}:
                return {
                    "success": True,
                    "body": {
                        "value": payload.get("value", ""),
                        "variablesReference": 0,
                    },
                }
            return {"success": True, "body": {"allThreadsContinued": True}}
        raise AssertionError(f"unexpected fake request {command!r}")

    def close(self) -> None:
        self.closed = True


class ProcessManagerTests(unittest.TestCase):
    def test_child_routes_are_virtualized_and_cleaned_by_continue(self) -> None:
        state = ProxySessionState()
        emitted: list[tuple[str, Mapping[str, Any]]] = []
        created: list[FakeChildTransport] = []
        with TemporaryDirectory() as directory:
            registry = Path(directory)

            def factory(
                command: Sequence[str],
                cwd: str,
                event_handler: Callable[[Message], None],
            ) -> FakeChildTransport:
                self.assertEqual(list(command), ["fake-codelldb"])
                self.assertEqual(cwd, "/workspace")
                transport = FakeChildTransport(event_handler)
                created.append(transport)
                return transport

            manager = ProcessManager(
                registry_path=registry,
                adapter_command=["fake-codelldb"],
                cwd="/workspace",
                state=state,
                emit_event=lambda event, body: emitted.append((event, body)),
                transport_factory=factory,
            )
            manager.add_breakpoints(
                {
                    "source": {"path": "/workspace/lib.rs"},
                    "breakpoints": [{"line": 6}],
                }
            )
            manager.mark_configuration_done()
            (registry / "ready-700").touch()
            manager._start_child({"pid": 700, "parentPid": 600})
            created[0].event_handler(
                {
                    "type": "event",
                    "event": "stopped",
                    "body": {"threadId": 701, "reason": "breakpoint"},
                }
            )

            self.assertEqual(manager.threads(), [{"id": 701, "name": "process 700: tid=701"}])
            self.assertEqual(state.process_id_for_thread(701), 700)
            self.assertTrue((registry / "attached-700").is_file())

            stack = manager.stack_trace(701, {})
            frame_id = stack["body"]["stackFrames"][0]["id"]
            self.assertGreaterEqual(frame_id, 1_000_000_000)
            self.assertIsNotNone(manager.native_frame_route(frame_id))
            native_stack_requests = [
                arguments
                for command, arguments in created[0].calls
                if command == "stackTrace"
            ]
            self.assertNotIn("levels", native_stack_requests[-1])

            scopes = manager.scopes(frame_id)
            reference = scopes["body"]["scopes"][0]["variablesReference"]
            self.assertGreaterEqual(reference, 1_500_000_000)
            self.assertEqual(
                manager.variables(reference)["body"]["variables"][0]["name"],
                "value",
            )
            self.assertEqual(manager.evaluate(frame_id, {"expression": "value + 1"})["body"]["result"], "21")

            manager.pause_thread(701)
            self.assertEqual(created[0].calls[-1], ("pause", {"threadId": 701}))
            manager.step_thread(
                "stepIn",
                701,
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
            self.assertIsNone(manager.native_frame_route(frame_id))

            stack = manager.stack_trace(701, {})
            frame_id = stack["body"]["stackFrames"][0]["id"]
            manager.continue_thread(701)
            self.assertIsNone(manager.native_frame_route(frame_id))
            self.assertTrue(created[0].calls[-1][0] == "continue")
            self.assertEqual(
                created[0].calls[-1][1],
                {"threadId": 701, "singleThread": False},
            )
            created[0].event_handler(
                {
                    "type": "event",
                    "event": "terminated",
                    "body": {},
                }
            )
            self.assertEqual(manager.threads(), [])
            self.assertNotIn(700, state.process_ids)
            self.assertTrue(created[0].closed)
            manager.close()
            self.assertTrue(created[0].closed)
        self.assertEqual(
            emitted,
            [
                (
                    "stopped",
                    {
                        "threadId": 701,
                        "reason": "breakpoint",
                        "systemProcessId": 700,
                    },
                ),
                ("terminated", {"systemProcessId": 700}),
            ],
        )

    def test_terminated_child_is_not_reattached_from_stale_registry_record(self) -> None:
        state = ProxySessionState()
        created: list[FakeChildTransport] = []
        with TemporaryDirectory() as directory:
            registry = Path(directory)

            def factory(
                command: Sequence[str],
                cwd: str,
                event_handler: Callable[[Message], None],
            ) -> FakeChildTransport:
                transport = FakeChildTransport(event_handler)
                created.append(transport)
                return transport

            manager = ProcessManager(
                registry_path=registry,
                adapter_command=["fake-codelldb"],
                cwd="/workspace",
                state=state,
                emit_event=lambda event, body: None,
                transport_factory=factory,
            )
            manager.add_breakpoints(
                {
                    "source": {"path": "/workspace/lib.rs"},
                    "breakpoints": [{"line": 6}],
                }
            )
            manager.mark_configuration_done()
            (registry / "ready-700").touch()
            record = {"pid": 700, "parentPid": 600}
            manager._start_child(record)
            created[0].event_handler(
                {"type": "event", "event": "terminated", "body": {}}
            )

            manager._start_child(record)

            self.assertEqual(len(created), 1)
            manager.close()

    def test_failed_attach_removes_the_partial_process_state(self) -> None:
        state = ProxySessionState()
        with TemporaryDirectory() as directory:
            registry = Path(directory)

            def factory(
                command: Sequence[str],
                cwd: str,
                event_handler: Callable[[Message], None],
            ) -> FakeChildTransport:
                transport = FakeChildTransport(event_handler)

                def fail_start(
                    *,
                    process_id: int,
                    breakpoints: Sequence[Mapping[str, Any]],
                ) -> None:
                    raise RuntimeError("attach denied")

                transport.start = fail_start  # type: ignore[method-assign]
                return transport

            output: list[str] = []
            manager = ProcessManager(
                registry_path=registry,
                adapter_command=["fake-codelldb"],
                cwd="/workspace",
                state=state,
                emit_event=lambda event, body: output.append(str(body["output"])),
                transport_factory=factory,
            )
            manager.add_breakpoints(
                {
                    "source": {"path": "/workspace/lib.rs"},
                    "breakpoints": [{"line": 6}],
                }
            )
            manager.mark_configuration_done()
            (registry / "ready-700").touch()
            manager._start_child({"pid": 700, "parentPid": 600})

            self.assertNotIn(700, state.process_ids)
            self.assertTrue(any("attach failed" in message for message in output))
            manager.close()


if __name__ == "__main__":
    unittest.main()
