from __future__ import annotations

import unittest

from prototype.adapter.coordinator import CoordinationError, ProcessCoordinator


class ProcessCoordinatorTests(unittest.TestCase):
    def test_process_tree_and_thread_bindings_are_isolated(self) -> None:
        coordinator = ProcessCoordinator()
        coordinator.register_process(100, engine="native")
        coordinator.register_process(200, parent_process_id=100, engine="python")
        coordinator.bind_native_thread(100, 101)
        coordinator.bind_native_thread(200, 201)
        coordinator.bind_python_thread(200, 201, 301)

        parent = coordinator.process(100)
        child = coordinator.process(200)
        assert parent is not None
        assert child is not None
        self.assertEqual(parent.children, {200})
        self.assertTrue(parent.native_connected)
        self.assertTrue(child.python_connected)
        self.assertEqual(child.threads[201].python_thread_id, 301)
        self.assertNotIn(201, parent.threads)

    def test_stop_owner_prevents_two_debuggers_from_resuming_same_process(self) -> None:
        coordinator = ProcessCoordinator()
        coordinator.register_process(100, engine="native")
        coordinator.bind_native_thread(100, 101)

        native = coordinator.acquire_stop(100, 101, "native")
        self.assertEqual(native.owner, "native")
        self.assertEqual(coordinator.execution_owner(100), "native")
        with self.assertRaisesRegex(CoordinationError, "native control"):
            coordinator.acquire_stop(100, 101, "python")
        with self.assertRaisesRegex(CoordinationError, "native owns"):
            coordinator.release_stop(100, "python")

        coordinator.release_stop(100, "native")
        python = coordinator.acquire_stop(100, 101, "python")
        self.assertEqual(python.owner, "python")

    def test_remove_child_detaches_parent_and_its_stop_lease(self) -> None:
        coordinator = ProcessCoordinator()
        coordinator.register_process(100)
        coordinator.register_process(200, parent_process_id=100)
        coordinator.acquire_stop(200, 201, "native")

        coordinator.remove_process(200)

        parent = coordinator.process(100)
        assert parent is not None
        self.assertEqual(parent.children, set())
        self.assertIsNone(coordinator.execution_owner(200))
        self.assertIsNone(coordinator.process(200))


if __name__ == "__main__":
    unittest.main()
