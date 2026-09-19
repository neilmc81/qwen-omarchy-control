"""Tests for the dormant pre-dispatch agent triage.

The safety contract these pin down:
  * `off` performs NO network call and never changes behaviour.
  * `log` records what would happen but always allows.
  * `enforce` applies the code policy, and any failure escalates rather than
    allowing.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qwen_omarchy_control import triage


def answers(route, confidence, destructive=0.0, clear=1.0, regenerable=0.0):
    return {
        "route": {"choice": route, "confidence": confidence,
                  "probabilities": {route: confidence}},
        "destructive": {"noul": destructive},
        "regenerable": {"noul": regenerable},
        "clear": {"noul": clear},
    }


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cfg = triage.CONFIG_FILE
        self._old_log = triage.LOG_FILE
        triage.CONFIG_FILE = Path(self._tmp.name) / "triage.json"
        triage.LOG_FILE = Path(self._tmp.name) / "triage.jsonl"

    def tearDown(self):
        triage.CONFIG_FILE = self._old_cfg
        triage.LOG_FILE = self._old_log
        self._tmp.cleanup()

    def test_default_is_off(self):
        self.assertEqual(triage.load_config()["mode"], "off")

    def test_invalid_mode_falls_back_to_off(self):
        triage.CONFIG_FILE.write_text(json.dumps({"mode": "bogus"}))
        self.assertEqual(triage.load_config()["mode"], "off")

    def test_save_roundtrip_and_permissions(self):
        cfg = triage.load_config()
        cfg["mode"] = "log"
        triage.save_config(cfg)
        self.assertEqual(triage.load_config()["mode"], "log")
        self.assertEqual(os.stat(triage.CONFIG_FILE).st_mode & 0o777, 0o600)


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(triage.DEFAULTS)

    def test_clear_agent_task_allows(self):
        verdict, _, _ = triage.decide(
            answers("agent_task", 0.95, destructive=0.05, clear=0.97), self.cfg)
        self.assertEqual(verdict, "allow")

    def test_garbage_refused(self):
        verdict, reason, _ = triage.decide(
            answers("garbage", 0.99, clear=0.02), self.cfg)
        self.assertEqual(verdict, "refuse")

    def test_unclear_refused_even_when_confident(self):
        # A confident route must not override "this was not a clear request".
        verdict, _, _ = triage.decide(
            answers("agent_task", 0.99, clear=0.30), self.cfg)
        self.assertEqual(verdict, "refuse")

    def test_destructive_held_for_confirmation(self):
        verdict, reason, _ = triage.decide(
            answers("agent_task", 0.95, destructive=0.9, clear=0.95), self.cfg)
        self.assertEqual(verdict, "confirm")
        self.assertIn("destructive", reason)

    def test_low_confidence_held(self):
        verdict, _, _ = triage.decide(
            answers("agent_task", 0.55, destructive=0.0, clear=0.9), self.cfg)
        self.assertEqual(verdict, "confirm")

    def test_non_agent_route_held(self):
        for route in ("desktop_command", "question", "unknown"):
            verdict, _, _ = triage.decide(
                answers(route, 0.95, destructive=0.0, clear=0.95), self.cfg)
            self.assertEqual(verdict, "confirm", route)

    def test_regenerable_work_is_not_destructive(self):
        # Regression: measured live, "run the tests" and "clean build
        # artifacts" scored ~0.6-0.68 on the raw destructive question alone and
        # would have been held. Data loss is destroys x (1 - regenerable).
        for destroys, regenerable in ((0.60, 0.95), (0.68, 0.97)):
            verdict, reason, details = triage.decide(
                answers("agent_task", 0.98, destructive=destroys,
                        clear=0.8, regenerable=regenerable), self.cfg)
            self.assertEqual(verdict, "allow", reason)
            self.assertLess(details["destructive"], self.cfg["destructiveConfirm"])

    def test_irreversible_destruction_is_held(self):
        verdict, reason, details = triage.decide(
            answers("agent_task", 0.99, destructive=0.98,
                    clear=0.8, regenerable=0.05), self.cfg)
        self.assertEqual(verdict, "confirm")
        self.assertGreater(details["destructive"], self.cfg["destructiveConfirm"])


class ModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cfg = triage.CONFIG_FILE
        self._old_log = triage.LOG_FILE
        triage.CONFIG_FILE = Path(self._tmp.name) / "triage.json"
        triage.LOG_FILE = Path(self._tmp.name) / "triage.jsonl"

    def tearDown(self):
        triage.CONFIG_FILE = self._old_cfg
        triage.LOG_FILE = self._old_log
        self._tmp.cleanup()

    def _set_mode(self, mode):
        cfg = triage.load_config()
        cfg["mode"] = mode
        triage.save_config(cfg)

    def test_off_makes_no_network_call(self):
        self._set_mode("off")
        with mock.patch.object(triage, "ask") as ask, \
                mock.patch.object(triage, "_read_key") as key:
            verdict = triage.evaluate("delete everything")
        ask.assert_not_called()
        key.assert_not_called()
        self.assertEqual(verdict.verdict, "allow")
        self.assertFalse(verdict.enforced)
        # Nothing is evaluated, so nothing is logged.
        self.assertEqual(triage.read_log(), [])

    def test_log_records_but_allows(self):
        self._set_mode("log")
        payload = {"answers": answers("garbage", 0.99, clear=0.0),
                   "usage": {"cost": 0.0001}}
        with mock.patch.object(triage, "ask", return_value=payload):
            verdict = triage.evaluate("you")
        self.assertEqual(verdict.verdict, "allow")   # effective
        self.assertFalse(verdict.enforced)
        rows = triage.read_log()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "refuse")   # what WOULD happen
        self.assertEqual(rows[0]["effective"], "allow")

    def test_enforce_applies_refuse(self):
        self._set_mode("enforce")
        payload = {"answers": answers("garbage", 0.99, clear=0.0), "usage": {}}
        with mock.patch.object(triage, "ask", return_value=payload):
            verdict = triage.evaluate("you")
        self.assertEqual(verdict.verdict, "refuse")
        self.assertTrue(verdict.enforced)

    def test_enforce_escalates_on_failure(self):
        self._set_mode("enforce")
        with mock.patch.object(triage, "ask",
                               side_effect=triage.TriageError("boom")):
            verdict = triage.evaluate("delete the project")
        # A broken Jev must never let a request through in enforce mode.
        self.assertEqual(verdict.verdict, "confirm")
        self.assertTrue(verdict.used_fallback)
        self.assertTrue(verdict.enforced)

    def test_log_failure_does_not_block(self):
        self._set_mode("log")
        with mock.patch.object(triage, "ask",
                               side_effect=triage.TriageError("boom")):
            verdict = triage.evaluate("delete the project")
        self.assertEqual(verdict.verdict, "allow")


class MaskingTest(unittest.TestCase):
    def test_secrets_are_masked(self):
        text = "use sk-or-v1-abcdef1234567890 and TOKEN=supersecretvalue"
        masked = triage.mask_secrets(text)
        self.assertNotIn("abcdef1234567890", masked)
        self.assertNotIn("supersecretvalue", masked)


class ReadKeyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.pop("OPENROUTER_API_KEY", None)

    def tearDown(self):
        if self._old is not None:
            os.environ["OPENROUTER_API_KEY"] = self._old
        self._tmp.cleanup()

    def test_env_wins(self):
        os.environ["OPENROUTER_API_KEY"] = "from-env"
        try:
            self.assertEqual(triage._read_key(triage.load_config()), "from-env")
        finally:
            os.environ.pop("OPENROUTER_API_KEY", None)

    def test_missing_key_raises(self):
        cfg = dict(triage.DEFAULTS)
        cfg["keyFile"] = str(Path(self._tmp.name) / "nope.env")
        with mock.patch.object(triage.Path, "home",
                               return_value=Path(self._tmp.name)):
            with self.assertRaises(triage.TriageError):
                triage._read_key(cfg)


class SummarizeTest(unittest.TestCase):
    def test_counts(self):
        rows = [
            {"verdict": "allow", "route": "agent_task", "cost_usd": 0.001},
            {"verdict": "confirm", "route": "agent_task", "used_fallback": True},
            {"verdict": "refuse", "route": "garbage"},
        ]
        summary = triage.summarize(rows)
        self.assertEqual(summary["entries"], 3)
        self.assertEqual(summary["by_verdict"]["allow"], 1)
        self.assertEqual(summary["fallbacks"], 1)
        self.assertAlmostEqual(summary["cost_usd"], 0.001)


if __name__ == "__main__":
    unittest.main()
