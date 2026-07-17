from __future__ import annotations

import json
import unittest

from .dap_support import DapError, DapStreamParser, encode_message


class DapFramingTests(unittest.TestCase):
    def test_fragmented_message(self) -> None:
        parser = DapStreamParser()
        wire = encode_message({"type": "event", "event": "initialized"})
        messages = []
        for byte in wire:
            messages.extend(parser.feed(bytes([byte])))
        self.assertEqual(messages, [{"type": "event", "event": "initialized"}])

    def test_multiple_messages_and_utf8_length(self) -> None:
        parser = DapStreamParser()
        first = {"type": "output", "body": {"output": "ümlaut"}}
        second = {"type": "response", "seq": 2, "success": True}
        messages = parser.feed(encode_message(first) + encode_message(second))
        self.assertEqual(messages, [first, second])

    def test_invalid_content_length_is_rejected(self) -> None:
        with self.assertRaisesRegex(DapError, "invalid Content-Length"):
            DapStreamParser().feed(b"Content-Length: nope\r\n\r\n{}")


class RequestRemappingTests(unittest.TestCase):
    def test_client_sequences_are_distinct_and_correlatable(self) -> None:
        requests = [
            {"seq": 1, "type": "request", "command": "initialize"},
            {"seq": 2, "type": "request", "command": "threads"},
        ]
        responses = [
            {"type": "response", "request_seq": 2, "success": True},
            {"type": "response", "request_seq": 1, "success": True},
        ]
        by_sequence = {request["seq"]: request for request in requests}
        self.assertEqual(by_sequence[responses[0]["request_seq"]]["command"], "threads")
        self.assertEqual(by_sequence[responses[1]["request_seq"]]["command"], "initialize")

    def test_json_payload_length_is_bytes_not_characters(self) -> None:
        wire = encode_message({"text": "π"})
        header, payload = wire.split(b"\r\n\r\n", 1)
        self.assertEqual(int(header.split(b":", 1)[1]), len(payload))
        self.assertEqual(json.loads(payload), {"text": "π"})


if __name__ == "__main__":
    unittest.main()
