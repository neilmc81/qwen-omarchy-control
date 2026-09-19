"""Tests for the trajectory audit (#14) and mid-sequence panic freeze.

The audit is log-only: these pin that it records honest outcomes, never raises,
and does not libel an unverifiable-but-working click as a failure.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qwen_omarchy_control import audit, panic


class AuditRecordTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = audit.LOG_FILE
        audit.LOG_FILE = Path(self._tmp.name) / "trajectory.jsonl"

    def tearDown(self):
        audit.LOG_FILE = self._old
        self._tmp.cleanup()

    def test_log_click_writes_one_ndjson_line(self):
        result = {
            "window_class": "nautilus", "window_title": "Home",
            "goal": "the Documents folder", "verification": "satisfied",
            "verified": True, "verification_reason": "window changed",
            "attempts": 1, "elapsed_s": 1.4, "cost_usd": 0.00004,
            "double": True, "takeover": "announced",
        }
        entry = audit.log_click(result)
        self.assertEqual(entry["outcome"], "satisfied")
        self.assertEqual(entry["app"], "nautilus")
        self.assertEqual(entry["latency_ms"], 1400)
        lines = audit.LOG_FILE.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["goal"], "the Documents folder")

    def test_log_file_is_private(self):
        audit.record({"tool": "click_element", "outcome": "satisfied"})
        self.assertEqual(os.stat(audit.LOG_FILE).st_mode & 0o777, 0o600)

    def test_record_never_raises_on_unwritable(self):
        audit.LOG_FILE = Path("/proc/definitely/not/writable/x.jsonl")
        self.assertIsNone(audit.record({"tool": "x", "outcome": "satisfied"}))

    def test_unknown_fields_are_dropped(self):
        entry = audit.record({"tool": "x", "outcome": "satisfied",
                              "secret": "sk-should-not-be-logged"})
        self.assertNotIn("secret", entry)
        self.assertNotIn("sk-should-not-be-logged", audit.LOG_FILE.read_text())


class AuditSummaryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = audit.LOG_FILE
        audit.LOG_FILE = Path(self._tmp.name) / "trajectory.jsonl"

    def tearDown(self):
        audit.LOG_FILE = self._old
        self._tmp.cleanup()

    def test_counts_and_per_app_rates(self):
        for outcome in ("satisfied", "satisfied", "unsatisfied", "unknown"):
            audit.record({"tool": "click_element", "app": "nautilus",
                          "outcome": outcome, "attempts": 1})
        audit.record({"tool": "click_element", "app": "inkscape",
                      "outcome": "satisfied", "attempts": 1})
        summary = audit.summarize(audit.read_log(limit=100))
        self.assertEqual(summary["actions"], 5)
        self.assertEqual(summary["satisfied"], 3)
        self.assertEqual(summary["unsatisfied"], 1)
        self.assertEqual(summary["unknown"], 1)
        nautilus = summary["by_app"]["nautilus"]
        self.assertEqual(nautilus["actions"], 4)
        self.assertEqual(nautilus["success_rate_of_verifiable"], 0.667)
        self.assertEqual(summary["by_app"]["inkscape"]["success_rate_of_verifiable"], 1.0)

    def test_unsatisfied_is_not_counted_as_failure(self):
        # Measured on this machine: a Nautilus grid-cell selection works but is
        # not exposed, so verification says 'unsatisfied'. It must not be called
        # a failure; success_rate_of_verifiable keeps the honest denominator.
        audit.record({"tool": "click_element", "app": "nautilus",
                      "outcome": "unsatisfied", "attempts": 1})
        summary = audit.summarize(audit.read_log())
        self.assertEqual(summary["success_rate_of_verifiable"], 0.0)
        self.assertEqual(summary["verifiable_rate"], 1.0)

    def test_empty_log(self):
        summary = audit.summarize([])
        self.assertEqual(summary["actions"], 0)
        self.assertIsNone(summary["success_rate_of_verifiable"])


class PanicFreezeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = panic.PANIC_FILE
        panic.PANIC_FILE = Path(self._tmp.name) / "stop"

    def tearDown(self):
        panic.PANIC_FILE = self._old
        self._tmp.cleanup()

    def test_guard_raises_when_frozen(self):
        panic.set_panic()
        self.assertTrue(panic.panicked())
        with self.assertRaises(panic.PanicError) as ctx:
            panic.guard("a click")
        self.assertIn("frozen", str(ctx.exception))
        self.assertIn("a click", str(ctx.exception))

    def test_guard_passes_when_clear(self):
        panic.guard("a click")  # must not raise

    def test_set_is_idempotent_and_clear_reports_change(self):
        self.assertTrue(panic.set_panic())
        self.assertFalse(panic.set_panic())
        self.assertTrue(panic.clear_panic())
        self.assertFalse(panic.clear_panic())


class OcrPathFreezeTest(unittest.TestCase):
    """The OCR input path must honour the freeze too, not just click_element."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = panic.PANIC_FILE
        panic.PANIC_FILE = Path(self._tmp.name) / "stop"
        panic.set_panic()
        from qwen_omarchy_control.desktop import DesktopController
        self.ctrl = DesktopController()

    def tearDown(self):
        panic.PANIC_FILE = self._old
        self._tmp.cleanup()

    def test_pointer_move_blocked(self):
        with self.assertRaises(panic.PanicError):
            self.ctrl.pointer_move(10, 20)

    def test_mouse_click_blocked_before_ydotool(self):
        with mock.patch("qwen_omarchy_control.desktop.run") as run:
            with self.assertRaises(panic.PanicError):
                self.ctrl.mouse_click("left")
        run.assert_not_called()

    def test_scroll_blocked(self):
        with self.assertRaises(panic.PanicError):
            self.ctrl.mouse_scroll("down")

    def test_type_text_blocked(self):
        with self.assertRaises(panic.PanicError):
            self.ctrl.type_text(None, "hello")


if __name__ == "__main__":
    unittest.main()
