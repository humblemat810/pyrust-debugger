"""Deterministic stdio DAP adapter used by proxy contract tests."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        name, value = line.decode("ascii").split(":", 1)
        headers[name.lower()] = value.strip()
    payload = sys.stdin.buffer.read(int(headers["content-length"]))
    return json.loads(payload)


def write_message(message: dict[str, Any]) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode())
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def response(
    request: dict[str, Any],
    *,
    body: dict[str, Any] | None = None,
    success: bool = True,
    message: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "seq": 9000 + request["seq"],
        "type": "response",
        "request_seq": request["seq"],
        "command": request["command"],
        "success": success,
    }
    if body is not None:
        result["body"] = body
    if message is not None:
        result["message"] = message
    return result


def run(mode: str) -> int:
    if mode == "malformed":
        sys.stdout.buffer.write(b"Content-Length: nope\r\n\r\n{}")
        sys.stdout.buffer.flush()
        return 0
    if mode == "incomplete":
        sys.stdout.buffer.write(b"Content-Length: 20\r\n\r\n{}")
        sys.stdout.buffer.flush()
        return 0
    if mode == "exit-early":
        print("fake adapter startup failed", file=sys.stderr, flush=True)
        return 23

    reverse_sequence = 700
    pending_reverse: dict[int, dict[str, Any]] = {}
    event_sequence = 5000

    while request := read_message():
        message_type = request.get("type")
        if message_type == "response":
            original = pending_reverse.pop(request["request_seq"])
            write_message(
                response(
                    original,
                    body={
                        "restoredRequestSeq": request["request_seq"],
                        "responseCommand": request["command"],
                    },
                )
            )
            continue

        command = request["command"]
        arguments = request.get("arguments", {})
        if command == "neverRespond":
            continue
        if command == "triggerReverse":
            pending_reverse[reverse_sequence] = request
            write_message(
                {
                    "seq": reverse_sequence,
                    "type": "request",
                    "command": "runInTerminal",
                    "arguments": {"kind": "integrated", "args": ["fixture"]},
                }
            )
            reverse_sequence += 1
            continue
        if command == "emitProcess":
            write_message(
                {
                    "seq": event_sequence,
                    "type": "event",
                    "event": "process",
                    "body": {"name": "fixture", "systemProcessId": 4242},
                }
            )
            event_sequence += 1
        elif command == "emitStopped":
            write_message(
                {
                    "seq": event_sequence,
                    "type": "event",
                    "event": "stopped",
                    "body": {"reason": "breakpoint", "threadId": 77},
                }
            )
            event_sequence += 1
        elif command == "emitContinued":
            write_message(
                {
                    "seq": event_sequence,
                    "type": "event",
                    "event": "continued",
                    "body": {"threadId": 77, "allThreadsContinued": True},
                }
            )
            event_sequence += 1

        if command == "stackTrace":
            body = {
                "stackFrames": [
                    {
                        "id": 11,
                        "name": "rust_inner",
                        "line": 6,
                        "column": 1,
                        "source": {"path": "/fixture/src/lib.rs"},
                    },
                    {
                        "id": 12,
                        "name": "rust_outer",
                        "line": 11,
                        "column": 1,
                        "source": {"path": "/fixture/src/lib.rs"},
                    },
                ],
                "totalFrames": 2,
                "receivedArguments": arguments,
            }
        elif command == "scopes":
            body = {
                "scopes": [
                    {
                        "name": "downstream-native",
                        "variablesReference": 1,
                        "expensive": False,
                    }
                ]
            }
        elif command == "evaluate":
            body = {
                "result": f"downstream:{arguments.get('expression', '')}",
                "variablesReference": 0,
                "receivedFrameId": arguments.get("frameId"),
            }
        else:
            body = {
                "seenRequestSeq": request["seq"],
                "receivedArguments": arguments,
            }

        write_message(response(request, body=body))
        if command == "disconnect":
            return 0

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("normal", "malformed", "incomplete", "exit-early"),
        default="normal",
    )
    args = parser.parse_args()
    return run(args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
