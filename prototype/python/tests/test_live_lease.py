from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest

from pyrust_stack.live_lease import run_live_lease


class LiveLeaseTests(unittest.TestCase):
    def test_live_frame_evaluation_import_and_assignment(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def selected() -> None:
                value = 20
                label = "secondary"
                run_live_lease(directory, "selected", __file__)
                self.assertEqual(value, 41)
                self.assertEqual(label, "secondary")

            thread = Thread(target=selected)
            thread.start()
            released = False
            try:
                ready = self._wait_json(root / "ready.json")
                selected_frame = ready["frames"][0]
                frame_id = selected_frame["id"]

                scopes = self._request(root, 1, "scopes", {"frameId": frame_id})
                local_reference = scopes["body"]["scopes"][0]["variablesReference"]
                variables = self._request(
                    root,
                    2,
                    "variables",
                    {"variablesReference": local_reference},
                )
                self.assertTrue(
                    any(
                        item["name"] == "value" and item["value"] == "20"
                        for item in variables["body"]["variables"]
                    )
                )
                evaluated = self._request(
                    root,
                    3,
                    "evaluate",
                    {
                        "frameId": frame_id,
                        "expression": "value * 2",
                        "context": "watch",
                    },
                )
                self.assertEqual(evaluated["body"]["result"], "40")
                imported = self._request(
                    root,
                    4,
                    "evaluate",
                    {
                        "frameId": frame_id,
                        "expression": "import math",
                        "context": "repl",
                    },
                )
                self.assertTrue(imported["success"])
                self.assertEqual(
                    self._request(
                        root,
                        5,
                        "evaluate",
                        {
                            "frameId": frame_id,
                            "expression": "math.factorial(5)",
                            "context": "watch",
                        },
                    )["body"]["result"],
                    "120",
                )
                assigned = self._request(
                    root,
                    6,
                    "setVariable",
                    {
                        "variablesReference": local_reference,
                        "name": "value",
                        "value": "41",
                    },
                )
                self.assertTrue(assigned["success"], assigned)
                self.assertEqual(assigned["body"]["value"], "41")
                self.assertEqual(
                    self._request(
                        root,
                        7,
                        "evaluate",
                        {
                            "frameId": frame_id,
                            "expression": "value",
                            "context": "watch",
                        },
                    )["body"]["result"],
                    "41",
                )
                self._request(root, 8, "continue", {})
                released = True
            finally:
                if not released and thread.is_alive() and (root / "ready.json").exists():
                    self._request(root, 99, "continue", {})
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_next_reenters_service_on_the_selected_frame(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result: list[int] = []

            def selected() -> None:
                value = 20
                run_live_lease(directory, "selected", __file__)
                value += 1
                result.append(value)

            thread = Thread(target=selected)
            thread.start()
            ready = self._wait_json(root / "ready.json")
            frame_id = ready["frames"][0]["id"]
            stepped = self._request(
                root,
                1,
                "next",
                {"frameId": frame_id},
            )
            self.assertTrue(stepped["success"], stepped)
            ready = self._wait_generation(root / "ready.json", 2)
            frame_id = ready["frames"][0]["id"]
            self.assertEqual(
                self._request(
                    root,
                    2,
                    "evaluate",
                    {
                        "frameId": frame_id,
                        "expression": "value",
                        "context": "watch",
                    },
                )["body"]["result"],
                "20",
            )
            self._request(root, 3, "continue", {})
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [21])

    def _request(
        self,
        root: Path,
        sequence: int,
        command: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        request = root / "request.json"
        temporary = root / ".request.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "seq": sequence,
                    "command": command,
                    "arguments": arguments,
                }
            ),
            encoding="utf-8",
        )
        temporary.replace(request)
        return self._wait_json(root / f"response-{sequence}.json")

    def _wait_json(self, path: Path) -> dict[str, object]:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                time.sleep(0.005)
                continue
            if isinstance(value, dict):
                return value
        self.fail(f"timed out waiting for {path}")

    def _wait_generation(
        self,
        path: Path,
        generation: int,
    ) -> dict[str, object]:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            value = self._wait_json(path)
            if value.get("generation") == generation:
                return value
            time.sleep(0.005)
        self.fail(f"timed out waiting for generation {generation}")
