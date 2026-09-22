"""Tests for the Tier 2 voice-native powers (#3, #5, #6, #7).

All offline: the Jev transport, cua-driver, the desktop and the clock are
mocked, so the policy of each module is pinned without a window, a network call,
or a real keystroke. The scripted backend records delivered actions, which is how
replay and sequencing are proven rather than asserted.
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from qwen_omarchy_control import jobs, macros, outcome, sequence, task, triage, vision


def cand(index, role="button", label="Save", enabled=True):
    return vision.Candidate(index=index, role=role, label=label, enabled=enabled,
                            frame={"x": 10 + index, "y": 20, "w": 40, "h": 20})


def fp(title, labels):
    return {"title": title, "elements": sorted((l, False, True) for l in labels),
            "count": len(labels)}


class ScriptedBackend:
    def __init__(self, observations):
        self._observations = list(observations)
        self.actions = []

    def observe(self, window_hint):
        if len(self._observations) > 1:
            return self._observations.pop(0)
        return self._observations[0]

    def execute(self, action, target, value, key, direction, window):
        self.actions.append((action, target.label if target else None, value, key, direction))
        return f"did {action}"


def obs(candidates, title="Window"):
    return task.Observation(
        window={"title": title, "app_name": title, "pid": 7, "window_id": 42},
        candidates=candidates,
        fingerprint=fp(title, [c.label for c in candidates]),
    )


class OutcomeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = outcome.audit.LOG_FILE
        outcome.audit.LOG_FILE = Path(self._tmp.name) / "traj.jsonl"
        self.cfg = {"enabled": True, "driver": "cua-driver",
                    "apiKeyEnv": "X", "endpoint": "http://x", "model": "m"}
        self._patches = [
            mock.patch.object(vision, "available", return_value=(True, "")),
            mock.patch.object(vision, "resolve_window",
                              return_value={"pid": 7, "window_id": 42, "title": "W"}),
            mock.patch.object(vision, "_run_driver",
                              return_value={"window_title": "W", "elements": [
                                  {"label": "Export"}]}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        outcome.audit.LOG_FILE = self._old
        self._tmp.cleanup()

    def _answer(self, choice, confidence=0.9):
        return {"answers": {"outcome": {"type": "choice", "choice": choice,
                                        "confidence": confidence}},
                "usage": {}}

    def test_spoken_is_composed_by_code_not_the_model(self):
        for choice, expected in outcome.OUTCOMES.items():
            with self.subTest(choice=choice):
                with mock.patch.object(triage, "ask", return_value=self._answer(choice)):
                    result = outcome.describe_outcome("export the file", cfg=self.cfg)
                self.assertEqual(result["status"], choice)
                self.assertEqual(result["spoken"], expected)
                # The sentence is plain language, not the raw enum token.
                self.assertNotEqual(result["spoken"], choice)
                self.assertTrue(result["spoken"][0].isupper())

    def test_uses_last_recorded_goal_when_none_given(self):
        outcome.audit.record({"tool": "click_element", "goal": "save the draft",
                              "app": "editor"})
        with mock.patch.object(triage, "ask", return_value=self._answer("done")):
            result = outcome.describe_outcome(cfg=self.cfg)
        self.assertEqual(result["goal"], "save the draft")

    def test_no_goal_and_no_history_is_honest(self):
        result = outcome.describe_outcome(cfg=self.cfg)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("don't have", result["spoken"])

    def test_degraded_window_is_unknown(self):
        with mock.patch.object(vision, "_run_driver",
                               return_value={"degraded": True}):
            result = outcome.describe_outcome("x", cfg=self.cfg)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("can't", result["spoken"])

    def test_unknown_choice_maps_to_unclear(self):
        with mock.patch.object(triage, "ask", return_value=self._answer("banana")):
            result = outcome.describe_outcome("x", cfg=self.cfg)
        self.assertEqual(result["status"], "unclear")


class SequenceTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {"enabled": True, "minConfidence": 0.6, "minIntervalMs": 0,
                    "audit": False}

    def _runner(self, results):
        calls = []

        def run(goal, **kwargs):
            calls.append(goal)
            return results.pop(0)

        return run, calls

    def test_all_steps_done(self):
        run, calls = self._runner([
            {"status": "done"}, {"status": "done"}, {"status": "done"}])
        result = sequence.do_sequence("tidy up", [
            {"goal": "a"}, {"goal": "b"}, {"goal": "c"}],
            cfg=self.cfg, _runner=run)
        self.assertEqual(result["status"], "done")
        self.assertEqual(calls, ["a", "b", "c"])
        self.assertEqual(result["steps_completed"], 3)
        self.assertIn("All 3 steps", result["spoken"])

    def test_stops_at_first_unverified_step(self):
        run, calls = self._runner([
            {"status": "done"}, {"status": "blocked"}, {"status": "done"}])
        result = sequence.do_sequence("tidy up", [
            {"goal": "make folder"}, {"goal": "move file"}, {"goal": "never"}],
            cfg=self.cfg, _runner=run)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(calls, ["make folder", "move file"])  # third never ran
        self.assertEqual(result["failed_step"], "move file")
        self.assertIn("1 of 3", result["spoken"])  # planned total, not attempted
        self.assertIn("move file", result["spoken"])

    def test_step_goal_required(self):
        with self.assertRaises(sequence.SequenceError):
            sequence.do_sequence("x", [{"verification": ["y"]}], cfg=self.cfg)

    def test_too_many_steps_refused(self):
        with self.assertRaises(sequence.SequenceError):
            sequence.do_sequence("x", [{"goal": str(i)} for i in range(20)],
                                 cfg=self.cfg)

    def test_result_is_voice_agnostic(self):
        run, _ = self._runner([{"status": "done"}])
        result = sequence.do_sequence("x", [{"goal": "y"}], cfg=self.cfg, _runner=run)
        self.assertIn("spoken", result)          # frontend reads this aloud
        self.assertIsInstance(result["results"], list)


class MacroTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = macros.MACROS_DIR
        macros.MACROS_DIR = Path(self._tmp.name) / "macros"
        self.cfg = {"enabled": True, "minConfidence": 0.6, "minIntervalMs": 0,
                    "driver": "cua-driver", "audit": False}
        self._patches = [
            mock.patch.object(vision, "available", return_value=(True, "")),
            mock.patch.object(vision, "_throttle", return_value=0.0),
            mock.patch.object(vision, "panic", create=True),
        ]
        for p in self._patches[:2]:
            p.start()

    def tearDown(self):
        macros._active = None
        for p in self._patches[:2]:
            p.stop()
        macros.MACROS_DIR = self._old_dir
        self._tmp.cleanup()

    def test_record_and_list_round_trip(self):
        macros.start("my chore")
        macros.record_step({"title": "Files", "app_name": "Files"},
                           "click", cand(0, label="Documents"), None, None, None)
        macros.record_step({"title": "Files", "app_name": "Files"},
                           "press_key", None, None, "Return", None)
        saved = macros.stop()
        self.assertEqual(saved["steps"], 2)
        listed = macros.list_macros()
        self.assertEqual(listed[0]["name"], "my chore")
        self.assertEqual(listed[0]["steps"], 2)

    def test_empty_recording_is_refused(self):
        macros.start("empty")
        with self.assertRaises(macros.MacroError):
            macros.stop()

    def test_name_is_validated(self):
        with self.assertRaises(macros.MacroError):
            macros.start("../../etc/passwd")

    def test_replay_retargets_by_label_and_verifies(self):
        macros.start("open docs")
        macros.record_step({"title": "Files", "app_name": "Files"},
                           "click", cand(0, label="Documents"), None, None, None)
        macros.stop()
        # Live window still offers "Documents" (label may have moved coords).
        live = [obs([cand(5, label="Documents")], title="Files"),
                obs([cand(5, label="Documents")], title="Documents")]
        backend = ScriptedBackend(live)
        with mock.patch.object(vision, "available", return_value=(True, "")):
            result = macros.replay("open docs", cfg=self.cfg, _backend=backend)
        self.assertEqual(result["status"], "done")
        self.assertEqual(backend.actions, [("click", "Documents", None, None, None)])
        self.assertIn("spoken", result)

    def test_replay_stops_when_target_missing(self):
        macros.start("do thing")
        macros.record_step({"title": "Files", "app_name": "Files"},
                           "click", cand(0, label="Documents"), None, None, None)
        macros.stop()
        backend = ScriptedBackend([obs([cand(1, label="Pictures")], title="Files")])
        result = macros.replay("do thing", cfg=self.cfg, _backend=backend)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("Documents", result["reason"])
        self.assertEqual(backend.actions, [])  # nothing delivered

    def test_replay_does_not_claim_success_on_no_change(self):
        macros.start("noop")
        macros.record_step({"title": "Files", "app_name": "Files"},
                           "click", cand(0, label="Documents"), None, None, None)
        macros.stop()
        # Same fingerprint before/after -> unsatisfied -> stop.
        same = obs([cand(0, label="Documents")], title="Files")
        backend = ScriptedBackend([same, same, same])
        with mock.patch.object(vision, "_verify_outcome",
                               return_value=("unsatisfied", "no change")):
            result = macros.replay("noop", cfg=self.cfg, _backend=backend)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("no observable change", result["reason"])

    def test_missing_macro_raises(self):
        with self.assertRaises(macros.MacroError):
            macros.load("nope")


class JobsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = jobs.JOBS_DIR
        jobs.JOBS_DIR = Path(self._tmp.name) / "jobs"

    def tearDown(self):
        jobs.JOBS_DIR = self._old
        self._tmp.cleanup()

    def test_job_lifecycle(self):
        def worker(job_id, stop):
            jobs.update(job_id, progress=1)
            jobs.finish(job_id, "met", spoken="done")

        job_id = jobs.start("demo", worker)
        for _ in range(50):
            record = jobs.read(job_id)
            if record and record.get("status") in jobs.TERMINAL:
                break
            time.sleep(0.02)
        record = jobs.read(job_id)
        self.assertEqual(record["status"], "met")
        self.assertEqual(record["spoken"], "done")

    def test_crashing_worker_becomes_failed(self):
        def worker(job_id, stop):
            raise RuntimeError("boom")

        job_id = jobs.start("demo", worker)
        for _ in range(50):
            record = jobs.read(job_id)
            if record and record.get("status") in jobs.TERMINAL:
                break
            time.sleep(0.02)
        record = jobs.read(job_id)
        self.assertEqual(record["status"], "failed")
        self.assertIn("boom", record["error"])

    def test_stop_sets_terminal(self):
        started = threading.Event()

        def worker(job_id, stop):
            started.set()
            stop.wait(5)
            jobs.finish(job_id, "stopped", spoken="stopped")

        job_id = jobs.start("demo", worker)
        started.wait(2)
        self.assertTrue(jobs.stop(job_id))


if __name__ == "__main__":
    unittest.main()
