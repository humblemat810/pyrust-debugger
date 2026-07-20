from __future__ import annotations

import subprocess
import sys
import unittest

from pyrust_stack.locals import read_python_locals


_CHILD_PROGRAM = """
import time

def inner(value):
    label = "remote"
    enabled = True
    ratio = 1.5
    print("ready", flush=True)
    while True:
        time.sleep(0.01)

def outer():
    marker = 7
    inner(20)

outer()
"""


class RemoteLocalsTests(unittest.TestCase):
    def test_reads_bounded_primitive_locals_newest_first(self) -> None:
        child = subprocess.Popen(
            [sys.executable, "-c", _CHILD_PROGRAM],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert child.stdout is not None
        self.assertEqual(child.stdout.readline().strip(), "ready")
        try:
            frames = read_python_locals(child.pid, child.pid)
        finally:
            child.terminate()
            child.wait(timeout=5)
            child.stdout.close()

        self.assertEqual([frame.name for frame in frames[:2]], ["inner", "outer"])
        self.assertEqual(
            frames[0].locals,
            {
                "value": 20,
                "label": "remote",
                "enabled": True,
                "ratio": 1.5,
            },
        )
        self.assertEqual(frames[1].locals, {"marker": 7})


if __name__ == "__main__":
    unittest.main()
