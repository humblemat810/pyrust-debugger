from __future__ import annotations

import unittest

from prototype.adapter.state import ProxySessionState, SyntheticFrameRegistry


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


if __name__ == "__main__":
    unittest.main()
