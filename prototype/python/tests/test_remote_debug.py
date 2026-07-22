from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from pyrust_stack.remote_debug import RemoteDebugError, queue_remote_debug_script


@unittest.skipUnless(
    sys.platform == "linux" and sys.version_info[:2] == (3, 14),
    "targeted remote debugging requires 64-bit Linux CPython 3.14",
)
class TargetedRemoteDebugTests(unittest.TestCase):
    def test_script_executes_on_selected_native_thread(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.py"
            result = root / "result.txt"
            injected = root / "injected.py"
            target.write_text(
                "import os\n"
                "import threading\n"
                "import time\n"
                "ready = threading.Event()\n"
                "def worker():\n"
                "    print(os.getpid(), threading.get_native_id(), flush=True)\n"
                "    ready.set()\n"
                "    while True:\n"
                "        time.sleep(0.005)\n"
                "threading.Thread(target=worker, daemon=True).start()\n"
                "ready.wait()\n"
                "while True:\n"
                "    time.sleep(0.005)\n",
                encoding="utf-8",
            )
            injected.write_text(
                "import threading\n"
                "from pathlib import Path\n"
                f"Path({str(result)!r}).write_text("
                "str(threading.get_native_id()), encoding='utf-8')\n",
                encoding="utf-8",
            )
            process = subprocess.Popen(
                [sys.executable, str(target)],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert process.stdout is not None
                process_id, worker_tid = map(
                    int,
                    process.stdout.readline().split(),
                )
                queue_remote_debug_script(
                    process_id,
                    worker_tid,
                    injected,
                )
                deadline = time.monotonic() + 5
                while not result.is_file() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(result.is_file())
                self.assertEqual(result.read_text(encoding="utf-8"), str(worker_tid))
            finally:
                process.terminate()
                process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()

    def test_script_executes_in_selected_subinterpreter(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / "subinterpreter-result.txt"
            injected = root / "injected.py"
            injected.write_text(
                "import threading\n"
                "from pathlib import Path\n"
                f"Path({str(result)!r}).write_text("
                "'sub:' + str(threading.get_native_id()), encoding='utf-8')\n",
                encoding="utf-8",
            )
            target = (
                "import _interpreters, threading, time\n"
                "interpreter = _interpreters.create()\n"
                "def launch():\n"
                "    _interpreters.exec(\n"
                "        interpreter,\n"
                '        "import os, threading, time\\n"\n'
                '        "def sub_inner(value):\\n"\n'
                '        "    print(os.getpid(), threading.get_native_id(), '
                'flush=True)\\n"\n'
                '        "    while True:\\n"\n'
                '        "        time.sleep(0.005)\\n"\n'
                '        "sub_inner(41)\\n",\n'
                "    )\n"
                "threading.Thread(target=launch, daemon=True).start()\n"
                "while True:\n"
                "    time.sleep(0.005)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", target],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert process.stdout is not None
                process_id, worker_tid = map(
                    int,
                    process.stdout.readline().split(),
                )
                queue_remote_debug_script(
                    process_id,
                    worker_tid,
                    injected,
                    expected_name="sub_inner",
                    expected_path="<script>",
                )
                deadline = time.monotonic() + 5
                while not result.is_file() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(result.is_file())
                self.assertEqual(
                    result.read_text(encoding="utf-8"),
                    f"sub:{worker_tid}",
                )
            finally:
                process.terminate()
                process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()

    def test_debugpy_handoff_rejects_subinterpreter_without_resuming_it(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            injected = root / "injected.py"
            injected.write_text("raise AssertionError('must not run')\n", encoding="utf-8")
            target = (
                "import _interpreters, threading, time\n"
                "interpreter = _interpreters.create()\n"
                "def launch():\n"
                "    _interpreters.exec(\n"
                "        interpreter,\n"
                '        "import os, threading, time\\n"\n'
                '        "def sub_inner(value):\\n"\n'
                '        "    print(os.getpid(), threading.get_native_id(), '
                'flush=True)\\n"\n'
                '        "    while True:\\n"\n'
                '        "        time.sleep(0.005)\\n"\n'
                '        "sub_inner(41)\\n",\n'
                "    )\n"
                "threading.Thread(target=launch, daemon=True).start()\n"
                "while True:\n"
                "    time.sleep(0.005)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", target],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert process.stdout is not None
                process_id, worker_tid = map(
                    int,
                    process.stdout.readline().split(),
                )
                with self.assertRaisesRegex(
                    RemoteDebugError,
                    "debugpy handoff is unavailable",
                ):
                    queue_remote_debug_script(
                        process_id,
                        worker_tid,
                        injected,
                        expected_name="sub_inner",
                        expected_path="<script>",
                        require_main_interpreter=True,
                    )
                self.assertIsNone(process.poll())
            finally:
                process.terminate()
                process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
