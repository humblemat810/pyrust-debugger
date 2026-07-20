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
