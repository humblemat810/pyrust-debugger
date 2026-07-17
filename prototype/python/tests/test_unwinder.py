from __future__ import annotations

from io import StringIO
import json
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pyrust_stack import StackReadError, read_python_stacks
from pyrust_stack.__main__ import main
from pyrust_stack import unwinder


_CHILD_PROGRAM = """
import ctypes
import time

libc = ctypes.CDLL(None, use_errno=True)
libc.prctl.argtypes = [
    ctypes.c_int,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_ulong,
]
if libc.prctl(0x59616D61, ctypes.c_ulong(-1).value, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), "could not permit test helper tracing")

def inner():
    print("ready", flush=True)
    while True:
        time.sleep(0.01)

def outer():
    inner()

outer()
"""


class RemoteUnwinderProcessTests(unittest.TestCase):
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

    def test_cli_reads_stack_as_json(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "pyrust_stack", str(self.child.pid)],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["threads"][0]["threadId"], self.child.pid)
        self.assertEqual(
            [frame["name"] for frame in payload["threads"][0]["frames"][:3]],
            ["inner", "outer", "<module>"],
        )

    def assert_expected_stack(self, stacks: object) -> None:
        stack_list = list(stacks)
        self.assertEqual(len(stack_list), 1)
        self.assertEqual(stack_list[0].thread_id, self.child.pid)
        self.assertEqual(
            [frame.name for frame in stack_list[0].frames[:3]],
            ["inner", "outer", "<module>"],
        )


class HelperContractTests(unittest.TestCase):
    def test_normalizes_newest_first_frames_and_os_thread_id(self) -> None:
        raw_threads = [
            SimpleNamespace(
                thread_id=1234,
                frame_info=[
                    SimpleNamespace(
                        funcname="newest", filename="/work/newest.py", lineno=30
                    ),
                    SimpleNamespace(
                        funcname="older", filename="/work/older.py", lineno=20
                    ),
                ],
            )
        ]
        remote_unwinder = unittest.mock.Mock()
        remote_unwinder.return_value.get_stack_trace.return_value = raw_threads

        with patch.object(unwinder, "RemoteUnwinder", remote_unwinder):
            stacks = read_python_stacks(os.getpid())

        self.assertEqual(remote_unwinder.call_args.args, (os.getpid(),))
        self.assertEqual(remote_unwinder.call_args.kwargs, {"all_threads": True})
        self.assertEqual(
            [stack.to_dict() for stack in stacks],
            [
                {
                    "threadId": 1234,
                    "frames": [
                        {"name": "newest", "path": "/work/newest.py", "line": 30},
                        {"name": "older", "path": "/work/older.py", "line": 20},
                    ],
                }
            ],
        )

    def test_rejects_malformed_remote_data(self) -> None:
        remote_unwinder = unittest.mock.Mock()
        remote_unwinder.return_value.get_stack_trace.return_value = [
            SimpleNamespace(thread_id=1234, frame_info=[SimpleNamespace()])
        ]

        with patch.object(unwinder, "RemoteUnwinder", remote_unwinder):
            with self.assertRaisesRegex(StackReadError, "malformed stack data") as caught:
                read_python_stacks(os.getpid())

        self.assertEqual(caught.exception.code, "malformed_data")

    def test_classifies_permission_failure(self) -> None:
        remote_unwinder = unittest.mock.Mock()
        remote_unwinder.return_value.get_stack_trace.side_effect = PermissionError()

        with patch.object(unwinder, "RemoteUnwinder", remote_unwinder):
            with self.assertRaises(StackReadError) as caught:
                read_python_stacks(os.getpid())

        self.assertEqual(caught.exception.code, "permission_denied")

    def test_classifies_target_exit_before_collection(self) -> None:
        with patch.object(unwinder.os, "kill", side_effect=ProcessLookupError()):
            with self.assertRaises(StackReadError) as caught:
                read_python_stacks(1234)

        self.assertEqual(caught.exception.code, "target_exited")

    def test_rejects_unsupported_helper_runtime(self) -> None:
        with patch.object(unwinder.sys, "version_info", (3, 13, 0)):
            with self.assertRaises(StackReadError) as caught:
                read_python_stacks(os.getpid())

        self.assertEqual(caught.exception.code, "unsupported_runtime")

    def test_cli_serializes_success_as_one_json_object(self) -> None:
        remote_unwinder = unittest.mock.Mock()
        remote_unwinder.return_value.get_stack_trace.return_value = [
            SimpleNamespace(
                thread_id=1234,
                frame_info=[
                    SimpleNamespace(
                        funcname="active", filename="/work/example.py", lineno=8
                    )
                ],
            )
        ]
        stdout = StringIO()

        with patch.object(unwinder, "RemoteUnwinder", remote_unwinder):
            exit_code = main([str(os.getpid())], stdout)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "ok": True,
                "threads": [
                    {
                        "threadId": 1234,
                        "frames": [
                            {
                                "name": "active",
                                "path": "/work/example.py",
                                "line": 8,
                            }
                        ],
                    }
                ],
            },
        )

    def test_cli_serializes_deterministic_errors(self) -> None:
        first = StringIO()
        second = StringIO()

        self.assertEqual(main(["not-a-pid"], first), 1)
        self.assertEqual(main(["not-a-pid"], second), 1)

        expected = (
            '{"error":{"code":"invalid_pid","message":"pid must be a positive integer"},'
            '"ok":false}\n'
        )
        self.assertEqual(first.getvalue(), expected)
        self.assertEqual(second.getvalue(), expected)


if __name__ == "__main__":
    unittest.main()
