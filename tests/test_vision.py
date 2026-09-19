"""Tests for precise element targeting (Cua accessibility tree + Jev selection).

All offline: cua-driver output and the Jev transport are mocked, so these pin
the parsing, candidate filtering, policy and failure modes without a network
call or a live window.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qwen_omarchy_control import triage, vision


def tree(*elements, snapshot="s00000001"):
    return {"snapshot_id": snapshot, "elements": list(elements),
            "window_id": 42, "pid": 7}


def element(index, role, label, actions=("click",), enabled=True):
    return {"element_index": index, "role": role, "label": label,
            "element_token": f"s00000001:{index}", "enabled": enabled,
            "actions": list(actions),
            "frame": {"x": 100 + index, "y": 200, "w": 50, "h": 20}}


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = vision.CONFIG_FILE
        vision.CONFIG_FILE = Path(self._tmp.name) / "vision.json"

    def tearDown(self):
        vision.CONFIG_FILE = self._old
        self._tmp.cleanup()

    def test_enabled_by_default(self):
        # The agent is meant to use this without a manual enable step; a kill
        # switch exists but defaults on, and the tool self-degrades instead.
        self.assertTrue(vision.load_config()["enabled"])

    def test_kill_switch_disables(self):
        vision.CONFIG_FILE.write_text(json.dumps({"enabled": False}))
        ok, reason = vision.available()
        self.assertFalse(ok)
        self.assertIn("disabled", reason)
        with self.assertRaises(vision.VisionError):
            vision.find_element("the Save button")

    def test_available_reports_missing_driver(self):
        vision.CONFIG_FILE.write_text(json.dumps({"enabled": True, "driver": "no-such-driver"}))
        ok, reason = vision.available()
        self.assertFalse(ok)
        self.assertIn("not installed", reason)


class CandidateTest(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(vision.DEFAULT_CONFIG)

    def test_keeps_labelled_actionable(self):
        cfg = self.cfg
        cands, snap = vision.candidates_from_tree(tree(
            element(0, "window", "Home"),
            element(1, "grid cell", "Documents. Folder"),
            element(2, "filler", ""),  # unlabelled structure dropped
        ), cfg)
        labels = [c.label for c in cands]
        self.assertIn("Documents. Folder", labels)
        self.assertNotIn("", labels)
        self.assertEqual(snap, "s00000001")

    def test_caps_candidate_count(self):
        cfg = dict(self.cfg, maxCandidates=3)
        tree_doc = tree(*[element(i, "push button", f"B{i}") for i in range(10)])
        cands, _ = vision.candidates_from_tree(tree_doc, cfg)
        self.assertEqual(len(cands), 3)

    def test_candidate_description_and_id(self):
        c = vision.Candidate(index=5, role="grid cell", label="Documents. Folder")
        self.assertEqual(c.to_id(), "e5")
        self.assertIn("grid cell", c.description)
        self.assertIn("Documents. Folder", c.description)

    def test_disabled_flagged(self):
        cands, _ = vision.candidates_from_tree(
            tree(element(0, "push button", "Save", enabled=False)), self.cfg)
        self.assertFalse(cands[0].enabled)
        self.assertIn("disabled", cands[0].description)


class JsonExtractionTest(unittest.TestCase):
    def test_extracts_json_from_noisy_output(self):
        text = "some warning\n{\"ok\": true, \"nested\": {\"a\": 1}}\ntrailing"
        self.assertEqual(vision._extract_json(text), {"ok": True, "nested": {"a": 1}})

    def test_handles_braces_in_strings(self):
        text = '{"label": "a } brace", "ok": true}'
        self.assertEqual(vision._extract_json(text)["label"], "a } brace")

    def test_none_when_absent(self):
        self.assertIsNone(vision._extract_json("no json here"))


class DriverTest(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(vision.DEFAULT_CONFIG, enabled=True)
        self._tmp = tempfile.TemporaryDirectory()
        self._old_log = triage.LOG_FILE
        triage.LOG_FILE = Path(self._tmp.name) / "t.jsonl"

    def tearDown(self):
        triage.LOG_FILE = self._old_log
        self._tmp.cleanup()

    def _patch_run(self, payloads):
        """Patch subprocess.run to return queued stdout per cua-driver call."""
        queue = list(payloads)

        def fake_run(argv, **kwargs):
            out = queue.pop(0) if queue else "{}"
            proc = mock.Mock()
            proc.returncode = 0
            proc.stdout = out
            proc.stderr = ""
            return proc

        return mock.patch("subprocess.run", side_effect=fake_run)

    def test_driver_refusal_is_a_clean_error(self):
        payload = json.dumps({"refusal": {"code": "snapshot_id_required"}})
        with self._patch_run([payload]):
            with self.assertRaises(vision.VisionError) as ctx:
                vision._run_driver(self.cfg, "click", {})
        self.assertIn("refused", str(ctx.exception))

    def test_degraded_tree_reports_fallback(self):
        windows = json.dumps({"windows": [
            {"pid": 7, "window_id": 42, "title": "OC", "app_name": "foot",
             "is_on_screen": True}]})
        degraded = json.dumps({"degraded": True, "elements": []})
        with self._patch_run([windows, degraded]):
            with self.assertRaises(vision.VisionError) as ctx:
                vision.find_element("the Save button", window_hint="foot",
                                    cfg=self.cfg)
        self.assertIn("accessibility tree", str(ctx.exception))

    def test_selection_full_path(self):
        windows = json.dumps({"windows": [
            {"pid": 7, "window_id": 42, "title": "Home", "app_name": "nautilus",
             "is_on_screen": True}]})
        state = json.dumps(tree(
            element(0, "grid cell", "Documents. Folder"),
            element(1, "push button", "Cancel"),
        ))
        jev = {"answers": {"candidate": {"choice": "e0", "confidence": 0.95,
                                          "probabilities": {"e0": 0.95, "e1": 0.05}}},
               "usage": {"cost": 0.00005}}
        with self._patch_run([windows, state]), \
                mock.patch.object(vision, "_jev_ask", return_value=jev):
            found = vision.find_element("the Documents folder", window_hint="nautilus",
                                        cfg=self.cfg)
        self.assertEqual(found["label"], "Documents. Folder")
        self.assertEqual(found["element_index"], 0)
        self.assertEqual(found["confidence"], 0.95)
        self.assertEqual(found["window_class"], "nautilus")

    def test_low_confidence_falls_back(self):
        windows = json.dumps({"windows": [
            {"pid": 7, "window_id": 42, "title": "Home", "app_name": "nautilus",
             "is_on_screen": True}]})
        state = json.dumps(tree(element(0, "push button", "Save")))
        jev = {"answers": {"candidate": {"choice": "e0", "confidence": 0.30}},
               "usage": {}}
        cfg = dict(self.cfg, minConfidence=0.60)
        with self._patch_run([windows, state]), \
                mock.patch.object(vision, "_jev_ask", return_value=jev):
            with self.assertRaises(vision.VisionError) as ctx:
                vision.find_element("the Save button", window_hint="nautilus", cfg=cfg)
        self.assertIn("not confident", str(ctx.exception))

    def test_unknown_candidate_id_is_rejected(self):
        candidates = [vision.Candidate(0, "push button", "Save")]
        jev = {"answers": {"candidate": {"choice": "e99", "confidence": 0.99}},
               "usage": {}}
        with mock.patch.object(vision, "_jev_ask", return_value=jev):
            with self.assertRaises(vision.VisionError):
                vision.select(self.cfg, "the Save button", {"title": "T"}, candidates)

    def test_click_uses_ydotool_not_cua(self):
        found = {"pid": 7, "window_id": 42, "window_title": "Home",
                 "window_class": "nautilus", "element_index": 0,
                 "element_token": "s1:0", "role": "grid cell",
                 "label": "Documents. Folder",
                 "frame": {"x": 100, "y": 200, "w": 50, "h": 20},
                 "confidence": 0.95, "candidate_count": 1, "cost_usd": 0.0,
                 "elapsed_s": 0.1}
        controller = mock.Mock()
        controller.focus_window.return_value = {"class": "nautilus"}
        controller.pointer_move.return_value = "pointer moved to 125,210"
        controller.mouse_click.return_value = "clicked left button"
        with mock.patch.object(vision, "find_element", return_value=found), \
                mock.patch.object(vision, "load_config", return_value=self.cfg), \
                mock.patch.object(vision, "_notify") as notify, \
                mock.patch.object(vision, "_window_fingerprint",
                                  side_effect=[{"title": "Home", "elements": []},
                                               {"title": "Documents", "elements": []}]), \
                mock.patch("qwen_omarchy_control.desktop.DesktopController",
                           return_value=controller), \
                mock.patch("time.sleep"):
            result = vision.click_element("the Documents folder", "nautilus")
        # Frame center: x 100+50//2=125, y 200+20//2=210
        controller.pointer_move.assert_called_once_with(125, 210)
        controller.mouse_click.assert_called_once_with("left")
        self.assertEqual(result["delivery"], "ydotool")
        self.assertEqual(result["clicked_at"], [125, 210])
        # The takeover is announced before any input is sent.
        notify.assert_called_once()
        self.assertIn("taking control", notify.call_args[0][0].lower())
        self.assertEqual(result["takeover"], "announced")
        # The outcome was verified from a real state change.
        self.assertTrue(result["verified"])
        self.assertEqual(result["verification"], "satisfied")

    def test_takeover_announcement_can_be_silenced(self):
        found = {"pid": 7, "window_id": 42, "window_title": "Home",
                 "window_class": "nautilus", "element_index": 0,
                 "element_token": "s1:0", "role": "grid cell", "label": "X",
                 "frame": {"x": 10, "y": 20, "w": 4, "h": 4},
                 "confidence": 0.9, "candidate_count": 1, "cost_usd": 0.0,
                 "elapsed_s": 0.1}
        controller = mock.Mock()
        controller.pointer_move.return_value = ""
        controller.mouse_click.return_value = ""
        cfg = dict(self.cfg, announceTakeover=False)
        with mock.patch.object(vision, "find_element", return_value=found), \
                mock.patch.object(vision, "load_config", return_value=cfg), \
                mock.patch.object(vision, "_notify") as notify, \
                mock.patch.object(vision, "_window_fingerprint",
                                  side_effect=[{"title": "A", "elements": []},
                                               {"title": "A", "elements": []}]), \
                mock.patch("qwen_omarchy_control.desktop.DesktopController",
                           return_value=controller), \
                mock.patch("time.sleep"):
            result = vision.click_element("x", "nautilus")
        notify.assert_not_called()
        self.assertEqual(result["takeover"], "silent")
        # No observable change is reported honestly, not as success.
        self.assertFalse(result["verified"])
        self.assertEqual(result["verification"], "unsatisfied")
        # And no second click: retries are off by default.
        controller.mouse_click.assert_called_once()
        self.assertEqual(result["attempts"], 1)


class VerifyOutcomeTest(unittest.TestCase):
    def test_title_change_satisfied(self):
        outcome, reason = vision._verify_outcome(
            {"title": "Home", "elements": []},
            {"title": "Documents", "elements": []})
        self.assertEqual(outcome, "satisfied")
        self.assertIn("Documents", reason)

    def test_element_or_selection_change_satisfied(self):
        before = {"title": "Home", "elements": [("Music. Folder", False, True)]}
        after = {"title": "Home", "elements": [("Music. Folder", True, True)]}
        outcome, _ = vision._verify_outcome(before, after)
        self.assertEqual(outcome, "satisfied")

    def test_no_change_unsatisfied(self):
        same = {"title": "Home", "elements": [("a", False, True)]}
        outcome, reason = vision._verify_outcome(same, dict(same))
        self.assertEqual(outcome, "unsatisfied")
        self.assertIn("nothing", reason)

    def test_vanished_window_is_unknown_not_failure(self):
        outcome, reason = vision._verify_outcome({"title": "H", "elements": []}, None)
        self.assertEqual(outcome, "unknown")
        self.assertIn("gone", reason)

    def test_unreadable_before_is_unknown(self):
        outcome, _ = vision._verify_outcome(None, {"title": "H", "elements": []})
        self.assertEqual(outcome, "unknown")


class ThrottleTest(unittest.TestCase):
    def setUp(self):
        self._old = vision._last_click_at
        vision._last_click_at = 0.0

    def tearDown(self):
        vision._last_click_at = self._old

    def test_first_click_does_not_wait(self):
        with mock.patch("time.sleep") as sleep, \
                mock.patch("time.monotonic", return_value=100.0):
            waited = vision._throttle(1200)
        self.assertEqual(waited, 0.0)
        sleep.assert_not_called()

    def test_second_click_waits_the_interval(self):
        with mock.patch("time.sleep") as sleep:
            with mock.patch("time.monotonic", return_value=100.0):
                vision._throttle(1200)
            with mock.patch("time.monotonic", return_value=100.2):
                waited = vision._throttle(1200)
        self.assertAlmostEqual(waited, 1.0, places=1)
        sleep.assert_called()

    def test_no_wait_when_interval_elapsed(self):
        with mock.patch("time.sleep") as sleep:
            with mock.patch("time.monotonic", return_value=100.0):
                vision._throttle(1200)
            with mock.patch("time.monotonic", return_value=105.0):
                waited = vision._throttle(1200)
        self.assertEqual(waited, 0.0)
        sleep.assert_not_called()


class PanicTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = vision.PANIC_FILE
        vision.PANIC_FILE = Path(self._tmp.name) / "stop"

    def tearDown(self):
        vision.PANIC_FILE = self._old
        self._tmp.cleanup()

    def test_panic_blocks_click_before_touching_mouse(self):
        vision.PANIC_FILE.write_text("")
        self.assertTrue(vision.panicked())
        with mock.patch("qwen_omarchy_control.desktop.DesktopController") as ctrl:
            with self.assertRaises(vision.VisionError) as ctx:
                vision.click_element("the Save button")
        self.assertIn("panic", str(ctx.exception))
        ctrl.assert_not_called()

    def test_clear_panic(self):
        vision.PANIC_FILE.write_text("")
        self.assertTrue(vision.clear_panic())
        self.assertFalse(vision.panicked())
        self.assertFalse(vision.clear_panic())


if __name__ == "__main__":
    unittest.main()
