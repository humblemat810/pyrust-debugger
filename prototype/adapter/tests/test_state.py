from __future__ import annotations

import io
import unittest

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
        )
        state.bind_thread(100, 101, name="Main thread")
        state.register_process(
            200,
            parent_process_id=100,
            display_name="worker-A",
            role="Python child process",
        )
        state.bind_thread(200, 201, name="Worker A")
        state.register_process(
            201,
            parent_process_id=100,
            display_name="worker-B",
            role="Python child process",
        )
        state.bind_thread(201, 202, name="Worker B")

        tree = state.process_tree()
        by_process = {item["processId"]: item for item in tree}
        self.assertEqual(by_process[100]["parentProcessId"], None)
        self.assertEqual(by_process[200]["parentProcessId"], 100)
        self.assertEqual(by_process[201]["parentProcessId"], 100)
        self.assertEqual(
            [thread["threadId"] for thread in by_process[100]["threads"]],
            [101],
        )
        self.assertEqual(
            [thread["threadId"] for thread in by_process[200]["threads"]],
            [201],
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

    def test_process_tree_uses_launch_metadata_for_native_process(self) -> None:
        state = ProxySessionState()
        state.set_default_process_metadata("Python process", "Python process")
        state.register_process(100)

        tree = state.process_tree()
        self.assertEqual(tree[0]["label"], "Python process")
        self.assertEqual(tree[0]["role"], "Python process")

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
