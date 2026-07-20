from __future__ import annotations

import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from prototype.adapter.coordinator import MAX_PROCESS_COMMAND_LENGTH
from prototype.adapter.process_manager import ProcessManager
from prototype.adapter.proxy import DapProxy
from prototype.adapter.state import ProcessSnapshot, ProxySessionState, SyntheticFrameRegistry


class SyntheticFrameRegistryTests(unittest.TestCase):
    def test_ids_are_stable_within_epoch_and_new_after_stop(self) -> None:
        registry = SyntheticFrameRegistry()
        registry.begin_epoch(1)
        first = registry.allocate(7, "python-0", {"name": "inner"})
        repeated = registry.allocate(7, "python-0", {"name": "inner"})

        registry.begin_epoch(2)
        second = registry.allocate(7, "python-0", {"name": "inner"})

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, second)
        self.assertEqual(registry.classify(first), "stale")
        self.assertEqual(registry.classify(second), "current")

    def test_ids_avoid_reserved_native_ids(self) -> None:
        registry = SyntheticFrameRegistry()
        registry.begin_epoch(1)
        synthetic = registry.allocate(
            7,
            "python-0",
            {},
            native_frame_ids=[2_147_483_647],
        )

        self.assertEqual(synthetic, 2_147_483_646)

    def test_current_native_ids_take_precedence_over_stale_synthetic_ids(self) -> None:
        registry = SyntheticFrameRegistry()
        registry.begin_epoch(1)
        old_synthetic = registry.allocate(7, "python-0", {})

        registry.begin_epoch(2)
        registry.reserve_native_ids([old_synthetic])

        self.assertEqual(registry.classify(old_synthetic), "native")

    def test_allocation_rejects_a_changed_epoch(self) -> None:
        registry = SyntheticFrameRegistry()
        registry.begin_epoch(2)

        with self.assertRaisesRegex(RuntimeError, "epoch changed"):
            registry.allocate(
                7,
                "python-0",
                {},
                expected_epoch=1,
            )

    def test_process_epochs_preserve_sibling_current_frames(self) -> None:
        registry = SyntheticFrameRegistry()
        registry.begin_epoch(1, process_id=100)
        first = registry.allocate(7, "python-0", {}, process_id=100)
        registry.begin_epoch(2, process_id=200)
        second = registry.allocate(8, "python-0", {}, process_id=200)

        registry.clear_current(process_id=100)

        self.assertEqual(registry.classify(first), "stale")
        self.assertEqual(registry.classify(second), "current")


class ProxySessionStateTests(unittest.TestCase):
    def test_proxy_accepts_a_shared_session_state(self) -> None:
        state = ProxySessionState()
        proxy = DapProxy(
            ["fake-downstream"],
            state=state,
            upstream_input=io.BytesIO(),
            upstream_output=io.BytesIO(),
        )
        self.assertIs(proxy.state, state)

    def test_process_and_stop_transitions(self) -> None:
        state = ProxySessionState()
        state.record_process_event(
            {
                "body": {
                    "systemProcessId": 4242,
                }
            }
        )
        self.assertEqual(state.process_id, 4242)

        self.assertEqual(state.on_stopped(), 1)
        self.assertTrue(state.is_stopped)
        state.on_continued()
        self.assertFalse(state.is_stopped)
        self.assertEqual(state.on_stopped(), 2)

    def test_tracks_threads_per_process_and_process_stop_state(self) -> None:
        state = ProxySessionState()
        state.record_process_event({"body": {"systemProcessId": 4242}})
        state.record_threads_response(
            {
                "body": {
                    "threads": [
                        {"id": 4242, "name": "main"},
                        {"id": 4243, "name": "worker"},
                    ]
                }
            }
        )

        self.assertEqual(state.process_id_for_thread(4243), 4242)
        self.assertEqual(
            state.process_snapshot(4242),
            ProcessSnapshot(
                process_id=4242,
                stop_epoch=0,
                is_stopped=False,
                thread_ids=frozenset({4242, 4243}),
            ),
        )

        self.assertEqual(
            state.on_stopped({"body": {"threadId": 4243}}),
            1,
        )
        self.assertEqual(
            state.process_snapshot(4242),
            ProcessSnapshot(
                process_id=4242,
                stop_epoch=1,
                is_stopped=True,
                thread_ids=frozenset({4242, 4243}),
                stopped_thread_ids=frozenset({4243}),
            ),
        )

        state.on_continued({"body": {"threadId": 4243}})
        snapshot = state.process_snapshot(4242)
        assert snapshot is not None
        self.assertFalse(snapshot.is_stopped)

    def test_process_tree_uses_parent_child_groups_without_thread_duplication(
        self,
    ) -> None:
        state = ProxySessionState()
        state.register_process(
            100,
            display_name="Python parent process",
            role="Python parent process",
            command="rust-parent --mode process-thread",
        )
        state.bind_thread(100, 101, name="Main thread")
        state.register_process(
            200,
            parent_process_id=100,
            display_name="worker-A",
            role="Python child process",
            command="python process_thread_worker.py process-A 20",
        )
        state.bind_thread(200, 201, name="process-A-worker-1")
        state.bind_thread(200, 203, name="process-A-worker-2")
        state.register_process(
            201,
            parent_process_id=100,
            display_name="worker-B",
            role="Python child process",
            command="python process_thread_worker.py process-B 40",
        )
        state.bind_thread(201, 202, name="Worker B")

        tree = state.process_tree()
        by_process = {item["processId"]: item for item in tree}
        self.assertEqual(by_process[100]["parentProcessId"], None)
        self.assertEqual(by_process[200]["parentProcessId"], 100)
        self.assertEqual(by_process[201]["parentProcessId"], 100)
        self.assertEqual(
            by_process[200]["command"],
            "python process_thread_worker.py process-A 20",
        )
        self.assertEqual(
            [thread["threadId"] for thread in by_process[100]["threads"]],
            [101],
        )
        self.assertEqual(
            [thread["threadId"] for thread in by_process[200]["threads"]],
            [201, 203],
        )
        self.assertEqual(
            [thread["name"] for thread in by_process[200]["threads"]],
            ["process-A-worker-1", "process-A-worker-2"],
        )
        self.assertEqual(
            [thread["threadId"] for thread in by_process[201]["threads"]],
            [202],
        )

        state.remove_process(200)
        after_exit = {item["processId"]: item for item in state.process_tree()}
        self.assertNotIn(200, after_exit)
        self.assertIn(100, after_exit)
        self.assertIn(201, after_exit)

    def test_process_tree_includes_stopped_thread_before_threads_response(self) -> None:
        state = ProxySessionState()
        state.register_process(100)
        state.on_stopped({"body": {"systemProcessId": 100, "threadId": 101}})

        tree = state.process_tree()
        self.assertEqual(tree[0]["threads"][0]["threadId"], 101)
        self.assertEqual(tree[0]["threads"][0]["name"], "Thread 101")
        self.assertTrue(tree[0]["threads"][0]["isStopped"])
        self.assertEqual(tree[0]["command"], "command unavailable")

    def test_process_tree_uses_launch_metadata_for_native_process(self) -> None:
        state = ProxySessionState()
        state.set_default_process_metadata(
            "Python process",
            "Python process",
            "/workspace/.venv/bin/python tests/acceptance/threaded_fixture_driver.py",
        )
        state.register_process(100)

        tree = state.process_tree()
        self.assertEqual(tree[0]["label"], "Python process")
        self.assertEqual(tree[0]["role"], "Python process")
        self.assertEqual(
            tree[0]["command"],
            "/workspace/.venv/bin/python tests/acceptance/threaded_fixture_driver.py",
        )

    def test_process_tree_bounds_and_normalizes_explicit_command_metadata(self) -> None:
        state = ProxySessionState()
        state.register_process(
            100,
            command=f"python\nworker.py\t{'x' * MAX_PROCESS_COMMAND_LENGTH}",
        )

        command = state.process_tree()[0]["command"]
        self.assertEqual(len(command), MAX_PROCESS_COMMAND_LENGTH)
        self.assertNotIn("\n", command)
        self.assertNotIn("\t", command)
        self.assertTrue(command.endswith("..."))

    def test_child_without_registry_command_does_not_inherit_parent_launch(self) -> None:
        state = ProxySessionState()
        state.set_default_process_metadata(
            "Rust process",
            "Rust process",
            "rust-parent --mode process-thread",
        )
        state.register_process(100)
        state.register_process(
            200,
            parent_process_id=100,
            display_name="process-A",
            role="Python child process",
            inherit_default_metadata=False,
        )

        by_process = {item["processId"]: item for item in state.process_tree()}
        self.assertEqual(by_process[100]["command"], "rust-parent --mode process-thread")
        self.assertEqual(by_process[200]["command"], "command unavailable")

    def test_child_threads_request_merges_two_native_names_into_tree(self) -> None:
        class FakeTransport:
            def start(self, *, process_id: int, breakpoints: object) -> None:
                del process_id, breakpoints

            def request(
                self,
                command: str,
                arguments: object = None,
            ) -> dict[str, object]:
                del arguments
                if command != "threads":
                    raise AssertionError(f"unexpected request {command}")
                return {
                    "success": True,
                    "body": {
                        "threads": [
                            {"id": 701, "name": "process-A-worker-1"},
                            {"id": 702, "name": "process-A-worker-2"},
                        ]
                    },
                }

            def close(self) -> None:
                pass

        state = ProxySessionState()
        with TemporaryDirectory() as directory:
            registry = Path(directory)
            (registry / "ready-700").touch()
            (registry / "child-700.json").write_text(
                (
                    '{"pid":700,"parentPid":600,"label":"process-A",'
                    '"role":"Python child process",'
                    '"command":"python process_thread_worker.py process-A 20",'
                    '"threads":['
                    '{"threadId":701,"name":"process-A-worker-1"},'
                    '{"threadId":702,"name":"process-A-worker-2"}'
                    "]}"
                ),
                encoding="utf-8",
            )
            manager = ProcessManager(
                registry_path=registry,
                adapter_command=["fake-codelldb"],
                cwd="/workspace",
                state=state,
                emit_event=lambda event, body: None,
                transport_factory=lambda command, cwd, handler: FakeTransport(),
            )
            try:
                manager._start_child(
                    {
                        "pid": 700,
                        "parentPid": 600,
                        "label": "process-A",
                        "role": "Python child process",
                        "command": "python process_thread_worker.py process-A 20",
                    }
                )
                state.on_stopped(
                    {"body": {"systemProcessId": 700, "threadId": 701}}
                )

                self.assertEqual(
                    manager.threads(),
                    [
                        {"id": 701, "name": "process 700: process-A-worker-1"},
                        {"id": 702, "name": "process 700: process-A-worker-2"},
                    ],
                )
                child = next(
                    process
                    for process in state.process_tree()
                    if process["processId"] == 700
                )
                self.assertEqual(
                    [
                        (thread["threadId"], thread["name"])
                        for thread in child["threads"]
                    ],
                    [
                        (701, "process-A-worker-1"),
                        (702, "process-A-worker-2"),
                    ],
                )
            finally:
                manager.close()

    def test_refreshing_child_threads_does_not_change_active_process(self) -> None:
        state = ProxySessionState()
        state.register_process(100)
        state.register_process(200)
        state.on_stopped({"body": {"systemProcessId": 100, "threadId": 101}})
        self.assertEqual(state.process_id, 100)
        state.bind_thread(200, 201, name="sibling", activate=False)
        self.assertEqual(state.process_id, 100)

    def test_records_codelldb_launch_output_as_process_fallback(self) -> None:
        state = ProxySessionState()
        state.record_output_event(
            {
                "body": {
                    "output": "Launched process 9876 from '/tmp/python'\n",
                }
            }
        )

        self.assertEqual(state.process_id, 9876)
        self.assertEqual(state.process_ids, frozenset({9876}))

    def test_process_event_can_preserve_parent_relationship(self) -> None:
        state = ProxySessionState()
        state.record_process_event({"body": {"systemProcessId": 100}})
        state.record_process_event(
            {"body": {"systemProcessId": 200, "parentProcessId": 100}}
        )

        parent = state.coordinator.process(100)
        child = state.coordinator.process(200)
        assert parent is not None
        assert child is not None
        self.assertEqual(parent.children, {200})
        self.assertEqual(child.parent_process_id, 100)


if __name__ == "__main__":
    unittest.main()
