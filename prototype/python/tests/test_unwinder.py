from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest

from pyrust_stack import read_python_stacks


_CHILD_PROGRAM = """
import time

def inner():
    print("ready", flush=True)
    while True:
        time.sleep(0.01)

def outer():
    inner()

outer()
"""


class RemoteUnwinderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.child = subprocess.Popen(
            [sys.executable, "-c", _CHILD_PROGRAM],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.child.stdout is not None
        self.assertEqual(self.child.stdout.readline().strip(), "ready")

    def tearDown(self) -> None:
        if self.child.poll() is None:
            try:
                os.kill(self.child.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            self.child.terminate()
            self.child.wait(timeout=5)
        if self.child.stdout is not None:
            self.child.stdout.close()

    def test_reads_stack_from_running_process(self) -> None:
        self.assert_expected_stack(read_python_stacks(self.child.pid))

    def test_reads_stack_from_stopped_process(self) -> None:
        os.kill(self.child.pid, signal.SIGSTOP)
        time.sleep(0.02)
        self.assert_expected_stack(read_python_stacks(self.child.pid))

    def assert_expected_stack(self, stacks: object) -> None:
        stack_list = list(stacks)
        self.assertEqual(len(stack_list), 1)
        self.assertEqual(stack_list[0].thread_id, self.child.pid)
        self.assertEqual(
            [frame.name for frame in stack_list[0].frames[:3]],
            ["inner", "outer", "<module>"],
        )


if __name__ == "__main__":
    unittest.main()
