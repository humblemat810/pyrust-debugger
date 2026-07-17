"""Incremental framing for Debug Adapter Protocol messages."""

from __future__ import annotations

from collections import deque
import json
from threading import Lock
from typing import Any, BinaryIO


DEFAULT_MAX_HEADER_BYTES = 16 * 1024
DEFAULT_MAX_CONTENT_BYTES = 16 * 1024 * 1024


class DapProtocolError(RuntimeError):
    """Raised when a peer sends malformed or incomplete DAP traffic."""


def encode_message(message: dict[str, Any]) -> bytes:
    payload = json.dumps(
        message,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload


class DapStreamParser:
    """Parse fragmented or coalesced DAP byte streams."""

    def __init__(
        self,
        *,
        max_header_bytes: int = DEFAULT_MAX_HEADER_BYTES,
        max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
    ) -> None:
        self._buffer = bytearray()
        self._expected_content_bytes: int | None = None
        self._max_header_bytes = max_header_bytes
        self._max_content_bytes = max_content_bytes

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        if not isinstance(data, bytes):
            raise TypeError("DAP parser input must be bytes")
        self._buffer.extend(data)
        messages: list[dict[str, Any]] = []

        while True:
            if self._expected_content_bytes is None:
                separator = self._buffer.find(b"\r\n\r\n")
                if separator < 0:
                    if len(self._buffer) > self._max_header_bytes:
                        raise DapProtocolError("DAP header exceeds configured limit")
                    return messages
                if separator > self._max_header_bytes:
                    raise DapProtocolError("DAP header exceeds configured limit")

                header = bytes(self._buffer[:separator])
                del self._buffer[: separator + 4]
                self._expected_content_bytes = self._parse_content_length(header)

            content_length = self._expected_content_bytes
            if len(self._buffer) < content_length:
                return messages

            payload = bytes(self._buffer[:content_length])
            del self._buffer[:content_length]
            self._expected_content_bytes = None
            messages.append(self._parse_payload(payload))

    def feed_eof(self) -> None:
        if self._expected_content_bytes is not None:
            raise DapProtocolError(
                "DAP stream ended before the declared message body was complete"
            )
        if self._buffer:
            raise DapProtocolError("DAP stream ended with an incomplete header")

    def _parse_content_length(self, header: bytes) -> int:
        try:
            text = header.decode("ascii")
        except UnicodeDecodeError as error:
            raise DapProtocolError("DAP headers must be ASCII") from error

        values: list[str] = []
        for line in text.split("\r\n"):
            if not line:
                continue
            if ":" not in line:
                raise DapProtocolError(f"malformed DAP header line: {line!r}")
            name, value = line.split(":", 1)
            if name.strip().lower() == "content-length":
                values.append(value.strip())

        if len(values) != 1:
            raise DapProtocolError("DAP message requires exactly one Content-Length")
        if not values[0].isascii() or not values[0].isdigit():
            raise DapProtocolError("DAP Content-Length must be a non-negative integer")

        content_length = int(values[0])
        if content_length > self._max_content_bytes:
            raise DapProtocolError("DAP message body exceeds configured limit")
        return content_length

    @staticmethod
    def _parse_payload(payload: bytes) -> dict[str, Any]:
        try:
            decoded = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DapProtocolError("DAP message body is not valid UTF-8") from error
        try:
            message = json.loads(decoded)
        except json.JSONDecodeError as error:
            raise DapProtocolError(f"DAP message body is not valid JSON: {error}") from error
        if not isinstance(message, dict):
            raise DapProtocolError("DAP message body must be a JSON object")
        return message


class DapReader:
    """Blocking DAP reader backed by an incremental parser."""

    def __init__(
        self,
        stream: BinaryIO,
        *,
        chunk_size: int = 64 * 1024,
        max_header_bytes: int = DEFAULT_MAX_HEADER_BYTES,
        max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
    ) -> None:
        self._stream = stream
        self._chunk_size = chunk_size
        self._parser = DapStreamParser(
            max_header_bytes=max_header_bytes,
            max_content_bytes=max_content_bytes,
        )
        self._messages: deque[dict[str, Any]] = deque()

    @property
    def has_pending_message(self) -> bool:
        return bool(self._messages)

    def read_message(self) -> dict[str, Any] | None:
        while not self._messages:
            read = getattr(self._stream, "read1", self._stream.read)
            chunk = read(self._chunk_size)
            if not chunk:
                self._parser.feed_eof()
                return None
            self._messages.extend(self._parser.feed(chunk))
        return self._messages.popleft()


class DapWriter:
    """Thread-safe DAP writer."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = Lock()

    def write_message(self, message: dict[str, Any]) -> None:
        framed = encode_message(message)
        with self._lock:
            self._stream.write(framed)
            self._stream.flush()
