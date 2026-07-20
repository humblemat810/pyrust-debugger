from __future__ import annotations

from pathlib import Path
import selectors
import subprocess
import sys
import time
from typing import Any, Callable
import unittest

from prototype.adapter.framing import DapReader, DapWriter


ROOT = Path(__file__).resolve().parents[3]
FAKE_ADAPTER = Path(__file__).with_name("fake_downstream.py")
SLICE_HOOKS = "prototype.adapter.tests.hook_fixtures:SliceHooks"
TIMEOUT_HOOKS = "prototype.adapter.tests.hook_fixtures:TimeoutHooks"


class ProxyProcess:
    def __init__(
        self,
        *,
        mode: str = "normal",
        hooks: str | None = None,
        request_timeout: float = 0.5,
        downstream_command: list[str] | None = None,
    ) -> None:
        command = [
            sys.executable,
            "-m",
            "prototype.adapter",
            "--request-timeout",
            str(request_timeout),
            "--shutdown-timeout",
            "0.25",
        ]
        if hooks is not None:
            command.extend(["--hooks", hooks])
        command.append("--")
        command.extend(
            downstream_command
            or [sys.executable, str(FAKE_ADAPTER), "--mode", mode]
        )
        self.process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.writer = DapWriter(self.process.stdin)
        self.reader = DapReader(self.process.stdout)

    def send(
        self,
        sequence: int,
        command: str,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        message: dict[str, Any] = {
            "seq": sequence,
            "type": "request",
            "command": command,
        }
        if arguments is not None:
            message["arguments"] = arguments
        self.writer.write_message(message)

    def send_response(
        self,
        sequence: int,
        request_sequence: int,
        command: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> None:
        message: dict[str, Any] = {
            "seq": sequence,
            "type": "response",
            "request_seq": request_sequence,
            "command": command,
            "success": True,
        }
        if body is not None:
            message["body"] = body
        self.writer.write_message(message)

    def read(self, timeout: float = 2.0) -> dict[str, Any]:
        assert self.process.stdout is not None
        if not self.reader.has_pending_message:
            selector = selectors.DefaultSelector()
            try:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                if not selector.select(timeout):
                    stderr = self.stderr_so_far()
                    raise TimeoutError(
                        f"timed out waiting for proxy DAP output; stderr={stderr!r}"
                    )
            finally:
                selector.close()
        message = self.reader.read_message()
        if message is None:
            raise EOFError(f"proxy output closed; stderr={self.stderr_so_far()!r}")
        return message

    def read_until(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout: float = 2.0,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        deadline = time.monotonic() + timeout
        seen: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            message = self.read(deadline - time.monotonic())
            seen.append(message)
            if predicate(message):
                return message, seen
        raise TimeoutError(f"message predicate not satisfied; seen={seen!r}")

    def wait(self, timeout: float = 2.0) -> int:
        return self.process.wait(timeout=timeout)

    def stderr_so_far(self) -> str:
        if self.process.poll() is None or self.process.stderr is None:
            return ""
        return self.process.stderr.read().decode("utf-8", errors="replace")

    def disconnect(self) -> None:
        try:
            if self.process.poll() is None:
                self.send(9999, "disconnect", {"terminateDebuggee": True})
                self.read_until(
                    lambda message: (
                        message.get("type") == "response"
                        and message.get("request_seq") == 9999
                    )
                )
                self.process.wait(timeout=2)
        except (BrokenPipeError, EOFError, TimeoutError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait(timeout=2)
        finally:
            for stream in (
                self.process.stdin,
                self.process.stdout,
                self.process.stderr,
            ):
                if stream is not None:
                    stream.close()


class ProxyContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proxies: list[ProxyProcess] = []

    def tearDown(self) -> None:
        for proxy in self.proxies:
            proxy.disconnect()

    def proxy(self, **kwargs: Any) -> ProxyProcess:
        proxy = ProxyProcess(**kwargs)
        self.proxies.append(proxy)
        return proxy

    def test_client_requests_are_remapped_and_responses_restored(self) -> None:
        proxy = self.proxy()
        proxy.send(41, "echo", {"value": "first"})
        first = proxy.read()
        proxy.send(99, "echo", {"value": "second"})
        second = proxy.read()

        self.assertEqual(first["request_seq"], 41)
        self.assertEqual(first["command"], "echo")
        self.assertEqual(first["body"]["seenRequestSeq"], 1)
        self.assertEqual(second["request_seq"], 99)
        self.assertEqual(second["body"]["seenRequestSeq"], 2)
        self.assertNotEqual(first["seq"], 9001)

    def test_downstream_requests_and_client_responses_are_correlated(self) -> None:
        proxy = self.proxy()
        proxy.send(20, "triggerReverse")
        reverse_request = proxy.read()

        self.assertEqual(reverse_request["type"], "request")
        self.assertEqual(reverse_request["command"], "runInTerminal")
        proxy.send_response(
            21,
            reverse_request["seq"],
            "runInTerminal",
            body={"processId": 123},
        )
        trigger_response = proxy.read()

        self.assertEqual(trigger_response["request_seq"], 20)
        self.assertEqual(trigger_response["body"]["restoredRequestSeq"], 700)
        self.assertEqual(
            trigger_response["body"]["responseCommand"],
            "runInTerminal",
        )

    def test_process_event_is_forwarded_and_recorded_transparently(self) -> None:
        proxy = self.proxy()
        proxy.send(30, "emitProcess")
        response, seen = proxy.read_until(
            lambda message: (
                message.get("type") == "response"
                and message.get("request_seq") == 30
            )
        )
        event = next(message for message in seen if message.get("event") == "process")

        self.assertTrue(response["success"])
        self.assertEqual(event["body"]["systemProcessId"], 4242)
        self.assertNotEqual(event["seq"], 5000)

    def test_hooks_can_request_full_native_stack_and_page_locally(self) -> None:
        proxy = self.proxy(hooks=SLICE_HOOKS)
        proxy.send(1, "emitStopped")
        stopped, _ = proxy.read_until(
            lambda message: message.get("event") == "stopped"
        )
        self.assertEqual(stopped["body"]["hookEpoch"], 1)

        proxy.send(
            2,
            "stackTrace",
            {"threadId": 77, "startFrame": 1, "levels": 2},
        )
        stack = proxy.read_until(
            lambda message: message.get("request_seq") == 2
        )[0]

        self.assertTrue(stack["success"])
        self.assertEqual(stack["body"]["totalFrames"], 3)
        self.assertEqual(
            [frame["name"] for frame in stack["body"]["stackFrames"]],
            ["rust_outer", "python_outer"],
        )
        self.assertEqual(stack["body"]["stackFrames"][0]["id"], 12)
        synthetic_id = stack["body"]["stackFrames"][1]["id"]
        self.assertGreater(synthetic_id, 12)

        proxy.send(3, "scopes", {"frameId": synthetic_id})
        scopes = proxy.read_until(
            lambda message: message.get("request_seq") == 3
        )[0]
        self.assertEqual(scopes["body"], {"scopes": []})

        proxy.send(
            4,
            "evaluate",
            {"frameId": 11, "expression": "native_value"},
        )
        native_evaluate = proxy.read_until(
            lambda message: message.get("request_seq") == 4
        )[0]
        self.assertTrue(native_evaluate["success"])
        self.assertEqual(
            native_evaluate["body"]["result"],
            "downstream:native_value",
        )
        self.assertEqual(native_evaluate["body"]["receivedFrameId"], 11)

        proxy.send(
            5,
            "evaluate",
            {"frameId": synthetic_id, "expression": "python_value"},
        )
        python_evaluate = proxy.read_until(
            lambda message: message.get("request_seq") == 5
        )[0]
        self.assertFalse(python_evaluate["success"])
        self.assertIn("unavailable", python_evaluate["message"])

    def test_continue_invalidates_synthetic_ids_and_next_stop_allocates_new(self) -> None:
        proxy = self.proxy(hooks=SLICE_HOOKS)
        proxy.send(10, "emitStopped")
        proxy.read_until(lambda message: message.get("event") == "stopped")
        proxy.send(11, "stackTrace", {"threadId": 77})
        first_stack = proxy.read_until(
            lambda message: message.get("request_seq") == 11
        )[0]
        first_id = first_stack["body"]["stackFrames"][-1]["id"]

        proxy.send(12, "emitContinued")
        continued, _ = proxy.read_until(
            lambda message: message.get("event") == "continued"
        )
        self.assertFalse(continued["body"]["hookSawStopped"])
        proxy.send(13, "scopes", {"frameId": first_id})
        stale = proxy.read_until(
            lambda message: message.get("request_seq") == 13
        )[0]
        self.assertFalse(stale["success"])
        self.assertIn("no longer valid", stale["message"])

        proxy.send(131, "variables", {"variablesReference": first_id})
        stale_variables = proxy.read_until(
            lambda message: message.get("request_seq") == 131
        )[0]
        self.assertFalse(stale_variables["success"])
        self.assertIn("no longer valid", stale_variables["message"])

        proxy.send(14, "emitStopped")
        stopped, _ = proxy.read_until(
            lambda message: message.get("event") == "stopped"
        )
        self.assertEqual(stopped["body"]["hookEpoch"], 2)
        proxy.send(15, "stackTrace", {"threadId": 77})
        second_stack = proxy.read_until(
            lambda message: message.get("request_seq") == 15
        )[0]
        second_id = second_stack["body"]["stackFrames"][-1]["id"]
        self.assertNotEqual(first_id, second_id)

    def test_internal_request_timeout_is_bounded_and_session_remains_usable(self) -> None:
        proxy = self.proxy(hooks=TIMEOUT_HOOKS, request_timeout=0.15)
        started = time.monotonic()
        proxy.send(50, "stackTrace", {"threadId": 77})
        timeout_response = proxy.read(timeout=1)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.8)
        self.assertFalse(timeout_response["success"])
        self.assertIn("timed out", timeout_response["message"])

        proxy.send(51, "echo")
        healthy_response = proxy.read()
        self.assertTrue(healthy_response["success"])
        self.assertEqual(healthy_response["request_seq"], 51)

    def test_clean_disconnect_exits_zero(self) -> None:
        proxy = self.proxy()
        proxy.send(60, "disconnect", {"terminateDebuggee": True})
        response = proxy.read()

        self.assertEqual(response["request_seq"], 60)
        self.assertEqual(proxy.wait(), 0)

    def test_malformed_downstream_protocol_fails_without_hanging(self) -> None:
        proxy = self.proxy(mode="malformed")
        self.assertEqual(proxy.wait(), 1)
        self.assertIn("downstream DAP protocol error", proxy.stderr_so_far())

    def test_incomplete_downstream_protocol_fails_without_hanging(self) -> None:
        proxy = self.proxy(mode="incomplete")
        self.assertEqual(proxy.wait(), 1)
        self.assertIn("declared message body", proxy.stderr_so_far())

    def test_early_downstream_exit_reports_adapter_layer(self) -> None:
        proxy = self.proxy(mode="exit-early")
        self.assertEqual(proxy.wait(), 1)
        stderr = proxy.stderr_so_far()
        self.assertIn("downstream adapter", stderr)
        self.assertIn("startup failed", stderr)

    def test_downstream_startup_failure_is_clear(self) -> None:
        proxy = self.proxy(downstream_command=["/definitely/missing/codelldb"])
        self.assertEqual(proxy.wait(), 1)
        self.assertIn("downstream startup failed", proxy.stderr_so_far())


if __name__ == "__main__":
    unittest.main()
