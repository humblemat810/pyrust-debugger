from __future__ import annotations

from io import BytesIO
import unittest

from prototype.adapter.framing import (
    DapProtocolError,
    DapReader,
    DapStreamParser,
    DapWriter,
    encode_message,
)


class DapStreamParserTests(unittest.TestCase):
    def test_fragmented_header_and_utf8_body(self) -> None:
        message = {
            "seq": 1,
            "type": "event",
            "event": "output",
            "body": {"output": "Rust -> Python: café"},
        }
        framed = encode_message(message)
        parser = DapStreamParser()
        parsed: list[dict[str, object]] = []

        for byte in framed:
            parsed.extend(parser.feed(bytes([byte])))

        self.assertEqual(parsed, [message])
        parser.feed_eof()

    def test_multiple_messages_in_one_read(self) -> None:
        first = {"seq": 1, "type": "event", "event": "initialized"}
        second = {
            "seq": 2,
            "type": "response",
            "request_seq": 8,
            "command": "threads",
            "success": True,
        }
        parser = DapStreamParser()

        self.assertEqual(
            parser.feed(encode_message(first) + encode_message(second)),
            [first, second],
        )

    def test_invalid_content_length_is_rejected(self) -> None:
        parser = DapStreamParser()
        with self.assertRaisesRegex(DapProtocolError, "Content-Length"):
            parser.feed(b"Content-Length: twelve\r\n\r\n{}")

    def test_duplicate_content_length_is_rejected(self) -> None:
        parser = DapStreamParser()
        with self.assertRaisesRegex(DapProtocolError, "exactly one"):
            parser.feed(
                b"Content-Length: 2\r\ncontent-length: 2\r\n\r\n{}"
            )

    def test_incomplete_body_is_rejected_at_eof(self) -> None:
        parser = DapStreamParser()
        parser.feed(b"Content-Length: 4\r\n\r\n{}")
        with self.assertRaisesRegex(DapProtocolError, "body"):
            parser.feed_eof()

    def test_content_limit_is_enforced(self) -> None:
        parser = DapStreamParser(max_content_bytes=3)
        with self.assertRaisesRegex(DapProtocolError, "configured limit"):
            parser.feed(b"Content-Length: 4\r\n\r\n")


class DapStreamIoTests(unittest.TestCase):
    def test_reader_retains_coalesced_messages(self) -> None:
        messages = [
            {"seq": 1, "type": "event", "event": "one"},
            {"seq": 2, "type": "event", "event": "two"},
        ]
        reader = DapReader(
            BytesIO(b"".join(encode_message(message) for message in messages))
        )

        self.assertEqual(reader.read_message(), messages[0])
        self.assertEqual(reader.read_message(), messages[1])
        self.assertIsNone(reader.read_message())

    def test_writer_uses_utf8_byte_length(self) -> None:
        output = BytesIO()
        message = {
            "seq": 1,
            "type": "event",
            "event": "output",
            "body": {"output": "é"},
        }
        DapWriter(output).write_message(message)

        parser = DapStreamParser()
        self.assertEqual(parser.feed(output.getvalue()), [message])


if __name__ == "__main__":
    unittest.main()
