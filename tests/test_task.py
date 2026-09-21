"""Tests for the bounded goal-level GUI task loop (#2 `do_gui_task`).

All offline: a scripted backend and a mocked Jev transport replace the live
desktop and the model, so the loop's policy is pinned without a window, a
network call, or a real keystroke. The scripted backend records every action
that was *actually delivered*, which is how the freshness guard and the
panic/budget terminals are proven rather than asserted.
"""

import json
import unittest
from unittest import mock

from qwen_omarchy_control import panic, task, triage, vision


def cand(index, role="button", label="Save", enabled=True):
    return vision.Candidate(index=index, role=role, label=label, enabled=enabled,
                            frame={"x": 10 + index, "y": 20, "w": 40, "h": 20})


def fp(title, labels):
    return {"title": title, "elements": sorted((l, False, True) for l in labels),
            "count": len(labels)}


class ScriptedBackend:
    """A deterministic backend: each observe() returns the next queued frame."""

    def __init__(self, observations):
        self._observations = list(observations)
        self.actions = []  # every delivered (action, target_label, value, key, dir)

    def observe(self, window_hint):
        if len(self._observations) > 1:
            return self._observations.pop(0)
        return self._observations[0]

    def execute(self, action, target, value, key, direction, window):
        self.actions.append((action, target.label if target else None,
                             value, key, direction))
        return f"did {action}"


def obs(candidates, title="Window", labels=None):
    return task.Observation(
        window={"title": title, "app_name": title, "pid": 7, "window_id": 42},
        candidates=candidates,
        fingerprint=fp(title, labels if labels is not None else [c.label for c in candidates]),
    )


def answers(operation, **kw):
    """Build a Jev payload with the chosen operation and any sub-answers."""
    ans = {"operation": {"type": "choice", "choice": operation, "confidence": 0.9,
                         "probabilities": {operation: 1.0}}}
    for key, value in kw.items():
        ans[key] = {"type": "choice", "choice": value, "confidence": 0.9,
                    "probabilities": {value: 1.0}}
    return {"answers": ans, "usage": {"cost": 0.0}}


class GuiTaskTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "enabled": True, "minConfidence": 0.60, "minIntervalMs": 0,
            "jevOutcomeThreshold": 0.7, "taskMaxActions": 8,
            "taskNoChangeLimit": 3, "audit": False,
        }
        self._patches = [
            mock.patch.object(vision, "available", return_value=(True, "")),
            mock.patch.object(vision, "_throttle", return_value=0.0),
            mock.patch.object(vision, "_verify_outcome",
                              side_effect=lambda b, a: self._diff(b, a)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    @staticmethod
    def _diff(before, after):
        if after is None:
            return "unknown", "window gone"
        if before != after:
            return "satisfied", "changed"
        return "unsatisfied", "no change"

    def _run(self, goal, backend, payloads, **kw):
        with mock.patch.object(triage, "ask", side_effect=payloads):
            return task.gui_task(goal, cfg=self.cfg, _backend=backend, **kw)

    def test_click_then_done(self):
        b = ScriptedBackend([
            obs([cand(0, label="Documents")], labels=["Documents"]),
            obs([cand(0, label="Documents")], title="Documents",
                labels=["file.txt"]),
            obs([cand(0, label="Documents")], title="Documents",
                labels=["file.txt"]),
        ])
        payloads = [answers("click", click_target="e0"), answers("done")]
        with mock.patch.object(task, "_verify_done", return_value=(True, "confirmed")):
            result = self._run("open the Documents folder", b, payloads)
        self.assertEqual(result["status"], "done")
        self.assertEqual(b.actions, [("click", "Documents", None, None, None)])
        self.assertEqual(result["actions_taken"], 1)

    def test_budget_exhausted_reports_needs_agent(self):
        o = obs([cand(0, label="Next")], labels=["Next"])
        b = ScriptedBackend([o, o, o, o, o, o, o, o, o])
        # Same action forever, always changing -> never trips the no-change guard.
        payloads = [answers("click", click_target="e0") for _ in range(8)]
        b.observe = lambda hint: obs([cand(0, label="Next")],
                                     title=f"T{b.actions.__len__()}", labels=["Next"])
        result = self._run("do the thing", b, payloads, max_actions=2)
        self.assertEqual(result["status"], "needs_agent")
        self.assertIn("budget", result["reason"])
        self.assertEqual(len(b.actions), 2)

    def test_no_change_streak_blocks(self):
        o = obs([cand(0, label="Save")], labels=["Save"])
        b = ScriptedBackend([o] * 10)
        payloads = [answers("click", click_target="e0") for _ in range(5)]
        result = self._run("save", b, payloads)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("no observable change", result["reason"])
        self.assertEqual(len(b.actions), 3)  # the configured streak

    def test_stale_target_is_discarded_not_replayed(self):
        # First observe offers e0 "Save"; the freshness re-read shows the element
        # has changed label, so the click must NOT be delivered.
        first = obs([cand(0, label="Save")], labels=["Save"])
        changed = obs([cand(0, label="Delete")], labels=["Delete"])
        settle = obs([cand(0, label="Delete")], labels=["Delete"])
        b = ScriptedBackend([first, changed, settle, settle])
        payloads = [
            answers("click", click_target="e0"),   # stale -> discarded
            answers("click", click_target="e0"),   # now against "Delete"
            answers("done"),
        ]
        with mock.patch.object(task, "_verify_done", return_value=(True, "ok")):
            result = self._run("act", b, payloads)
        self.assertEqual(result["status"], "done")
        # The stale decision delivered nothing; only the second click landed.
        self.assertEqual([a[0] for a in b.actions], ["click"])

    def test_panic_stops_between_steps(self):
        o = obs([cand(0, label="Save")], labels=["Save"])
        b = ScriptedBackend([o] * 6)
        payloads = [answers("click", click_target="e0")]
        with mock.patch.object(panic, "panicked", return_value=True), \
                mock.patch.object(triage, "ask", side_effect=payloads):
            result = task.gui_task("go", cfg=self.cfg, _backend=b)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("panic", result["reason"])
        self.assertEqual(b.actions, [])

    def test_degraded_window_returns_needs_agent(self):
        d = task.Observation(window={"title": "Chrome", "pid": 1, "window_id": 2},
                             candidates=[], fingerprint=None,
                             degraded="no accessibility tree")
        b = ScriptedBackend([d])
        with mock.patch.object(triage, "ask") as ask:
            result = task.gui_task("do it", cfg=self.cfg, _backend=b)
        self.assertEqual(result["status"], "needs_agent")
        self.assertIn("accessibility tree", result["reason"])
        ask.assert_not_called()  # never even asks the model

    def test_press_key_is_validated_against_safe_list(self):
        o = obs([cand(0, label="OK")], labels=["OK"])
        b = ScriptedBackend([o] * 5)
        payloads = [answers("press_key", press_key_value="Return"), answers("done")]
        with mock.patch.object(task, "_verify_done", return_value=(True, "ok")):
            result = self._run("press return", b, payloads)
        self.assertEqual(result["status"], "done")
        self.assertEqual(b.actions, [("press_key", None, None, "Return", None)])

    def test_model_choice_outside_menu_is_rejected(self):
        o = obs([cand(0, label="Save")], labels=["Save"])
        b = ScriptedBackend([o] * 3)
        bad = answers("click", click_target="e99")  # not offered
        with mock.patch.object(triage, "ask", return_value=bad):
            result = task.gui_task("go", cfg=self.cfg, _backend=b)
        self.assertEqual(result["status"], "needs_agent")
        self.assertIn("not offered", result["reason"])
        self.assertEqual(b.actions, [])

    def test_click_none_is_blocked(self):
        o = obs([cand(0, label="Save")], labels=["Save"])
        b = ScriptedBackend([o] * 3)
        payloads = [answers("click", click_target="none")]
        result = self._run("click the missing thing", b, payloads)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(b.actions, [])

    def test_model_can_hand_back(self):
        o = obs([cand(0, label="Save")], labels=["Save"])
        b = ScriptedBackend([o] * 3)
        for terminal in ("blocked", "needs_agent"):
            with self.subTest(terminal=terminal):
                result = self._run("go", b, [answers(terminal)])
                self.assertEqual(result["status"], terminal)

    def test_type_text_never_invents_value(self):
        o = obs([cand(0, role="entry", label="Name")], labels=["Name"])
        b = ScriptedBackend([o] * 5)
        payloads = [
            answers("type_text", type_text_input="name", type_text_target="e0"),
            answers("done"),
        ]
        with mock.patch.object(task, "_verify_done", return_value=(True, "ok")):
            result = self._run("type the name", b, payloads,
                               inputs={"name": "Neil"})
        self.assertEqual(result["status"], "done")
        self.assertEqual(b.actions, [("type_text", "Name", "Neil", None, None)])

    def test_missing_goal_raises(self):
        with self.assertRaises(task.TaskError):
            task.gui_task("   ", cfg=self.cfg, _backend=ScriptedBackend([obs([])]))

    def test_safe_wrapper_degrades(self):
        with mock.patch.object(vision, "available", return_value=(False, "no key")):
            result = task.gui_task_safe("go", cfg=self.cfg)
        self.assertEqual(result["status"], "needs_agent")
        self.assertIn("no key", result["reason"])


class VisionBackendBindingTest(unittest.TestCase):
    """The target window is resolved once and then bound for the whole task.

    Regression: re-resolving by title each step broke as soon as the task
    changed the window. Measured live, opening a folder retitles the window
    ('guitask-demo' -> 'alpha'), so the second observe failed with "no window
    matches" *after* the click had already worked. Window identity, not title,
    must persist across steps.
    """

    def setUp(self):
        self.cfg = {"driver": "cua-driver", "driverTimeoutMs": 1000,
                    "maxCandidates": 40, "enabled": True}
        self.window = {"pid": 7, "window_id": 42, "title": "guitask-demo",
                       "class": "nautilus"}

    def test_observe_resolves_the_window_only_once(self):
        tree = {"snapshot_id": "s1", "elements": [
            {"element_index": 0, "role": "grid cell", "label": "alpha. Folder",
             "element_token": "s1:0", "enabled": True, "actions": ["click"],
             "frame": {"x": 1, "y": 2, "w": 3, "h": 4}},
        ]}
        backend = task.VisionBackend(self.cfg)
        with mock.patch.object(vision, "resolve_window",
                               return_value=self.window) as resolve, \
                mock.patch.object(vision, "_run_driver", return_value=tree), \
                mock.patch.object(vision, "_window_fingerprint",
                                  return_value={"title": "x", "elements": [],
                                                "count": 0}):
            backend.observe("guitask-demo")
            backend.observe("guitask-demo")  # title now differs in reality
        # Resolved exactly once, not once per observe.
        self.assertEqual(resolve.call_count, 1)


if __name__ == "__main__":
    unittest.main()
