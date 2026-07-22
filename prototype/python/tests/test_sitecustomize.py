from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest

from prototype.python.sitecustomize import _wait_for_client


class _FakeDebugpy:
    def __init__(self) -> None:
        self.connected = False

    def is_client_connected(self) -> bool:
        return self.connected


class SitecustomizeTests(unittest.TestCase):
    def test_wait_returns_when_debugpy_connects(self) -> None:
        debugpy = _FakeDebugpy()
        with TemporaryDirectory() as directory:
            endpoint = Path(directory) / "debugpy-100.json"

            def connect() -> None:
                time.sleep(0.025)
                debugpy.connected = True
                endpoint.with_suffix(".ready").touch()

            Thread(target=connect, daemon=True).start()
            self.assertTrue(_wait_for_client(debugpy, endpoint, 1.0))

    def test_connection_without_configured_ready_marker_does_not_release(self) -> None:
        debugpy = _FakeDebugpy()
        debugpy.connected = True
        with TemporaryDirectory() as directory:
            endpoint = Path(directory) / "debugpy-100.json"
            self.assertFalse(_wait_for_client(debugpy, endpoint, 0.05))

    def test_wait_returns_when_coordinator_marks_attach_failure(self) -> None:
        debugpy = _FakeDebugpy()
        with TemporaryDirectory() as directory:
            endpoint = Path(directory) / "debugpy-100.json"
            endpoint.with_suffix(".failed").touch()
            started = time.monotonic()
            self.assertFalse(_wait_for_client(debugpy, endpoint, 1.0))
            self.assertLess(time.monotonic() - started, 0.25)

    def test_wait_is_bounded_without_coordinator(self) -> None:
        debugpy = _FakeDebugpy()
        with TemporaryDirectory() as directory:
            endpoint = Path(directory) / "debugpy-100.json"
            started = time.monotonic()
            self.assertFalse(_wait_for_client(debugpy, endpoint, 0.05))
            self.assertLess(time.monotonic() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
