"""Unit coverage for manual breakpoint-hold configuration."""

from __future__ import annotations

import os
from unittest import TestCase
from unittest.mock import patch

from .process_thread_worker import (
    BREAKPOINT_HOLD_TIMEOUT_ENV,
    DEFAULT_WORKER_TIMEOUT_SECONDS,
    _breakpoint_hold_timeout_seconds,
)


class ProcessThreadWorkerTimeoutTests(TestCase):
    def test_default_timeout_stays_bounded_for_automation(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                _breakpoint_hold_timeout_seconds(),
                int(DEFAULT_WORKER_TIMEOUT_SECONDS),
            )

    def test_manual_launch_can_extend_breakpoint_hold(self) -> None:
        with patch.dict(
            os.environ,
            {BREAKPOINT_HOLD_TIMEOUT_ENV: "3600"},
            clear=True,
        ):
            self.assertEqual(_breakpoint_hold_timeout_seconds(), 3600)

    def test_invalid_timeout_is_rejected(self) -> None:
        for value in ("0", "-1", "not-a-number", "1.5"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {BREAKPOINT_HOLD_TIMEOUT_ENV: value},
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    _breakpoint_hold_timeout_seconds()
