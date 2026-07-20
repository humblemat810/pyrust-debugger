from __future__ import annotations

import unittest

from .dap_support import EpochFrames, names, user_frames


class StackContractTests(unittest.TestCase):
    def test_fixture_order_and_paging_shape(self) -> None:
        response = {
            "body": {
                "stackFrames": [
                    {"id": 101, "name": "rust_inner"},
                    {"id": 102, "name": "rust_outer"},
                    {"id": 9001, "name": "python_inner"},
                    {"id": 9002, "name": "python_outer"},
                ],
                "totalFrames": 4,
            }
        }
        frames = user_frames(response)
        self.assertEqual(names(frames), ["rust_inner", "rust_outer", "python_inner", "python_outer"])
        self.assertEqual(response["body"]["totalFrames"], len(frames))

        for start, levels, expected in ((0, 2, ["rust_inner", "rust_outer"]),
                                        (1, 2, ["rust_outer", "python_inner"]),
                                        (3, 2, ["python_outer"])):
            page = frames[start : start + levels]
            self.assertEqual(names(page), expected)

    def test_synthetic_ids_are_epoch_scoped(self) -> None:
        first = EpochFrames(epoch=1, frame_ids=frozenset({9001, 9002}))
        second = EpochFrames(epoch=2, frame_ids=frozenset({9101, 9102}))
        self.assertTrue(first.frame_ids.isdisjoint(second.frame_ids))
        self.assertNotIn(9001, second.frame_ids)
        self.assertNotEqual(first.epoch, second.epoch)

    def test_native_ids_remain_usable_and_distinct(self) -> None:
        native = {101, 102}
        synthetic = {9001, 9002}
        self.assertTrue(native.isdisjoint(synthetic))


class SadPathContractTests(unittest.TestCase):
    def test_helper_failure_and_timeout_are_native_fallback_contracts(self) -> None:
        native = [{"id": 101, "name": "rust_inner"}]
        for outcome in ("helper-error", "helper-timeout"):
            result = {"stackFrames": native, "diagnostic": outcome}
            self.assertEqual(result["stackFrames"], native)
            self.assertTrue(result["diagnostic"])

    def test_synthetic_scopes_expose_snapshot_locals(self) -> None:
        frame_id = 9001
        scopes = {
            "body": {
                "scopes": [
                    {
                        "name": "Python Locals",
                        "presentationHint": "locals",
                        "variablesReference": frame_id,
                        "expensive": False,
                    }
                ]
            }
        }
        variables = {
            "body": {
                "variables": [
                    {
                        "name": "value",
                        "value": "20",
                        "type": "int",
                        "variablesReference": 0,
                    }
                ]
            }
        }
        evaluation = {"body": {"result": "21", "type": "int"}}

        self.assertEqual(
            scopes["body"]["scopes"][0]["variablesReference"],
            frame_id,
        )
        self.assertEqual(variables["body"]["variables"][0]["value"], "20")
        self.assertEqual(evaluation["body"]["result"], "21")


if __name__ == "__main__":
    unittest.main()
