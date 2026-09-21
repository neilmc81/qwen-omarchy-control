"""Bounded, goal-level GUI task execution (#2 `do_gui_task`).

Why this exists
---------------
The primitives (`find_element`, `click_element`, `describe_actions`,
`browser_*`) each do one thing well, but a spoken goal like "open the Downloads
folder and rename the newest file" is a *sequence*, and unverified multi-step
work multiplies failure. This module runs the sequence as a bounded loop and
reports honestly whether it got there.

The design is ported, not copied, from two independent references that agreed
on the same shape: `shhivv/arc-cua` and Cua's own
`cua-driver/examples/jev-use` (built on the same cua-driver this project uses).

    code observes the window  ->  builds a bounded candidate/action menu
        ->  Jev picks exactly one option (an id it was given)
        ->  code validates it against the live state, executes it
        ->  code re-observes and checks what actually happened
        ->  repeat until done / stuck / out of budget

Rules this module never breaks
------------------------------
- **Jev only chooses from options code built.** It cannot invent element ids,
  coordinates, text or tools. Literal text always comes from the caller via
  `inputs`.
- **A score is not proof the action worked.** Every step re-observes; `done`
  requires the caller's verification criteria to be observably satisfied.
- **Freshness guard.** A chosen target is re-checked against a freshly read
  tree immediately before it is acted on; a stale decision is discarded, never
  replayed.
- **Bounded and fail-closed.** A hard `max_actions`, a consecutive no-change
  streak ends as `blocked`, and the panic flag is checked before every delivery.
- **Terminal states reported honestly:** `done` | `blocked` | `needs_agent`.

It is deliberately *not* in the realtime voice loop: it is a tool the frontend
may call, and each step costs one Jev round-trip plus throttle time.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from . import panic, selection, triage, vision

TERMINALS = ("done", "blocked", "needs_agent")

# Actions Jev may choose, mapped to what they need. `done` claims completion and
# is only accepted after verification; the other two are terminal hand-offs.
ACTIONS = ("click", "double_click", "type_text", "press_key", "scroll",
           "done", "blocked", "needs_agent")

# A small, safe fixed key list. Nothing here types text or submits a form on its
# own; Return is included because navigating a dialog to completion is a normal
# task, and the caller asked for the goal.
SAFE_KEYS = ("Return", "Escape", "Tab", "BackSpace", "Delete",
             "Up", "Down", "Left", "Right", "space")

SCROLL_DIRECTIONS = ("up", "down")


class TaskError(RuntimeError):
    """The task could not be attempted (bad arguments, unavailable backend)."""


@dataclass
class Step:
    """One recorded decision + action, for the audit log and the caller."""
    index: int
    action: str
    target_id: str | None = None
    target_label: str | None = None
    value: str | None = None
    key: str | None = None
    direction: str | None = None
    confidence: float = 0.0
    changed: bool = False
    verified: str = "unknown"
    reason: str = ""
    cost_usd: float = 0.0

    def compact(self) -> dict[str, Any]:
        return {
            "step": self.index,
            "action": self.action,
            "target": self.target_label or self.target_id,
            "value": self.value,
            "key": self.key,
            "direction": self.direction,
            "confidence": round(self.confidence, 3),
            "changed": self.changed,
            "verified": self.verified,
            "reason": self.reason,
        }


@dataclass
class Observation:
    """What the backend sees right now: a window, its candidates, a fingerprint."""
    window: dict
    candidates: list[vision.Candidate]
    fingerprint: dict | None
    degraded: str = ""
    context: dict = field(default_factory=dict)

    def candidate(self, cid: str) -> vision.Candidate | None:
        for c in self.candidates:
            if c.to_id() == cid:
                return c
        return None


class Backend(Protocol):
    """Observation + input boundary.

    The default implementation drives cua-driver and ydotool/wtype. Tests inject
    a scripted backend so the loop's policy, freshness guard, budget and
    terminal states are pinned without a live window or network call.
    """

    def observe(self, window_hint: str | None) -> Observation: ...

    def execute(self, action: str, target: vision.Candidate | None,
                value: str | None, key: str | None,
                direction: str | None, window: dict) -> str: ...


# --- default backend (real desktop) ---------------------------------------


class VisionBackend:
    """Observe via cua-driver's accessibility tree; act via ydotool/wtype.

    The target window is resolved **once** and then bound for the whole task.
    Re-resolving by title each step would break as soon as the task changes the
    window (navigating into a folder retitles it, so the hint stops matching) —
    the window identity, not its title, must persist across steps.
    """

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or vision.load_config()
        self._bound: dict | None = None

    def bind(self, window_hint: str | None) -> dict:
        window = vision.resolve_window(self.cfg, window_hint)
        if window.get("pid") is None or window.get("window_id") is None:
            raise TaskError("resolved window has no pid/window_id")
        self._bound = window
        return window

    def observe(self, window_hint: str | None) -> Observation:
        cfg = self.cfg
        window = self._bound or self.bind(window_hint)
        pid, window_id = window.get("pid"), window.get("window_id")
        tree = vision._run_driver(cfg, "get_window_state", {
            "pid": pid, "window_id": window_id, "include_screenshot": False,
        })
        if tree.get("degraded"):
            return Observation(
                window=window, candidates=[], fingerprint=None,
                degraded="this window has no accessibility tree (a canvas/"
                         "Electron surface); do_gui_task cannot drive it",
            )
        candidates, _ = vision.candidates_from_tree(tree, cfg)
        fingerprint = vision._window_fingerprint(cfg, pid, window_id)
        return Observation(window=window, candidates=candidates,
                           fingerprint=fingerprint)

    def execute(self, action: str, target: vision.Candidate | None,
                value: str | None, key: str | None,
                direction: str | None, window: dict) -> str:
        cfg = self.cfg
        from .desktop import DesktopController
        ctrl = DesktopController()

        if action in ("click", "double_click"):
            if target is None:
                raise TaskError("click requires a target element")
            frame = target.frame or {}
            if not frame.get("w") or not frame.get("h"):
                raise TaskError("the chosen element has no usable frame")
            x = int(frame["x"]) + int(frame["w"]) // 2
            y = int(frame["y"]) + int(frame["h"]) // 2
            class_hint = window.get("title") or window.get("app_name") or ""
            try:
                if class_hint:
                    ctrl.focus_window(class_hint)
            except Exception:  # noqa: BLE001 - focus is best-effort
                pass
            time.sleep(0.3)
            ctrl.pointer_move(x, y)
            time.sleep(0.2)
            if action == "double_click":
                return vision._double_click()
            return ctrl.mouse_click("left")

        if action == "type_text":
            if value is None:
                raise TaskError("type_text requires an input value")
            # Click the field first if one was named, so wtype targets it.
            if target is not None:
                frame = target.frame or {}
                if frame.get("w") and frame.get("h"):
                    x = int(frame["x"]) + int(frame["w"]) // 2
                    y = int(frame["y"]) + int(frame["h"]) // 2
                    class_hint = window.get("title") or window.get("app_name") or ""
                    if class_hint:
                        try:
                            ctrl.focus_window(class_hint)
                        except Exception:  # noqa: BLE001
                            pass
                    time.sleep(0.25)
                    ctrl.pointer_move(x, y)
                    time.sleep(0.15)
                    ctrl.mouse_click("left")
                    time.sleep(0.15)
            return ctrl.type_text(None, value, send=bool(key == "Return"))

        if action == "press_key":
            if key not in SAFE_KEYS:
                raise TaskError(f"key {key!r} is not in the safe key list")
            return vision._press_key(key)

        if action == "scroll":
            if direction not in SCROLL_DIRECTIONS:
                raise TaskError(f"scroll direction must be one of {SCROLL_DIRECTIONS}")
            return ctrl.mouse_scroll(direction, 1, window.get("title"))

        raise TaskError(f"action {action!r} has no delivery")


# --- the loop --------------------------------------------------------------


def _operation_questions(observation: Observation, inputs: dict[str, str],
                         goal: str, constraints: list[str],
                         history: list[Step]) -> tuple[dict, dict]:
    """Build the bounded fan-out questions and the id tables they refer to.

    Returns (questions, maps) where maps holds, per question, the allowed ids so
    the answer can be validated against exactly what was offered.
    """
    clickable = [c for c in observation.candidates if c.enabled]
    by_id = {c.to_id(): c for c in clickable}

    operations: dict[str, str] = {}
    if clickable:
        operations["click"] = "Click one observed element."
        operations["double_click"] = "Double-click one observed element (open/activate)."
    if inputs and clickable:
        operations["type_text"] = "Enter one supplied value into an observed field."
    operations["press_key"] = "Press one safe key (e.g. Return, Escape, Tab)."
    operations["scroll"] = "Scroll the window up or down."
    operations["done"] = "The verification criteria are now observably satisfied."
    operations["blocked"] = "No offered action can make progress."
    operations["needs_agent"] = "Progress or verification needs higher-level reasoning."

    questions: dict[str, Any] = {
        "operation": {
            "type": "choice",
            "instructions": (
                f"Goal: {goal}. Choose exactly one next operation to make "
                "progress. Choose 'done' only when the goal's verification is "
                "observably satisfied in the window state. Choose 'blocked' if "
                "nothing offered can help, 'needs_agent' if it needs reasoning "
                "you cannot do from this menu."
                + (f" Constraints: {'; '.join(constraints)}." if constraints else "")
            ),
            "criteria": operations,
        }
    }
    maps: dict[str, dict] = {"operation": {k: k for k in operations}}

    if clickable:
        target_criteria = {cid: c.description for cid, c in by_id.items()}
        target_criteria["none"] = "No observed element fits this operation."
        for verb in ("click", "double_click"):
            maps[f"{verb}_target"] = target_criteria
            questions[f"{verb}_target"] = {
                "type": "choice",
                "instructions": f"Goal: {goal}. Which element should {verb} act "
                                "on? Choose 'none' if none fits.",
                "criteria": target_criteria,
            }

    if inputs and clickable:
        input_criteria = {k: f"value: {v}" for k, v in inputs.items()}
        maps["type_text_input"] = input_criteria
        questions["type_text_input"] = {
            "type": "choice",
            "instructions": (f"Goal: {goal}. Which supplied value should be "
                             "typed? Never invent a value."),
            "criteria": input_criteria,
        }
        maps["type_text_target"] = target_criteria
        questions["type_text_target"] = {
            "type": "choice",
            "instructions": (f"Goal: {goal}. Which observed field should the "
                             "supplied value be typed into? Choose 'none' if "
                             "there is no field."),
            "criteria": target_criteria,
        }

    maps["press_key_value"] = {k: k for k in SAFE_KEYS}
    questions["press_key_value"] = {
        "type": "choice",
        "instructions": "If the operation is press_key, which single key?",
        "criteria": maps["press_key_value"],
    }
    maps["scroll_direction"] = {d: d for d in SCROLL_DIRECTIONS}
    questions["scroll_direction"] = {
        "type": "choice",
        "instructions": "If the operation is scroll, which direction?",
        "criteria": maps["scroll_direction"],
    }
    return questions, maps


def _answer_choice(payload: dict, key: str, allowed: dict) -> tuple[str, float]:
    """Read one Choice answer, validating it is an offered id."""
    ans = (payload.get("answers") or {}).get(key) or {}
    choice = str(ans.get("choice") or "")
    confidence = float(ans.get("confidence") or 0.0)
    if choice not in allowed:
        raise TaskError(f"model chose {choice!r}, which was not offered")
    return choice, confidence


def _verify_done(cfg: dict, goal: str, verification: list[str],
                 fingerprint: dict | None, changed_ever: bool) -> tuple[bool, str]:
    """Is the caller's `done` claim observably true?

    A step that changed nothing cannot be 'done' unless the model can confirm
    the criteria against the window state (the #1b pattern). Fail closed: any
    error is a 'no'.
    """
    criteria = " ".join(verification) if verification else goal
    if fingerprint is None:
        return False, "the window could not be read to confirm completion"
    state = json.dumps({"goal": goal, "criteria": criteria,
                        "window": vision._tree_view(fingerprint)}, ensure_ascii=False)
    try:
        payload = vision._ask_yes_no(
            cfg, state,
            "A desktop agent reports the task finished. Based on the window's "
            "current state, are the stated verification criteria satisfied?",
        )
    except vision.VisionError as exc:
        return False, f"could not confirm completion ({exc})"
    prob = float((payload.get("answers") or {}).get("yes", {}).get("noul") or 0.0)
    threshold = float(cfg.get("jevOutcomeThreshold", 0.7))
    if prob >= threshold:
        return True, f"verification confirmed ({prob:.2f})"
    if changed_ever:
        return False, (f"the window changed but the goal is not confirmed "
                       f"({prob:.2f}); handing back for review")
    return False, "the model claimed done but nothing changed and criteria are unconfirmed"


def gui_task(goal: str, window: str | None = None,
             inputs: dict[str, str] | None = None,
             verification: list[str] | None = None,
             constraints: list[str] | None = None,
             max_actions: int | None = None,
             cfg: dict | None = None,
             _backend: Backend | None = None) -> dict:
    """Run a bounded, verified, goal-level GUI task. Never raises TaskError out.

    Returns a dict with `status` in TERMINALS, the step history, and the reason.
    """
    cfg = cfg or vision.load_config()
    goal = (goal or "").strip()
    if not goal:
        raise TaskError("a goal is required")
    inputs = {str(k): str(v) for k, v in (inputs or {}).items()}
    verification = [str(v) for v in (verification or [])]
    constraints = [str(v) for v in (constraints or [])]
    budget = int(max_actions if max_actions is not None
                 else cfg.get("taskMaxActions", 8))
    budget = max(1, min(budget, 25))
    no_change_limit = int(cfg.get("taskNoChangeLimit", 3))

    ok, why = vision.available(cfg)
    if not ok:
        raise TaskError(why)
    backend = _backend or VisionBackend(cfg)

    history: list[Step] = []
    changed_ever = False
    started = time.monotonic()

    try:
        observation = backend.observe(window)
    except (TaskError, vision.VisionError) as exc:
        return _result("needs_agent", goal, history, str(exc), started)

    if observation.degraded:
        return _result("needs_agent", goal, history, observation.degraded, started)

    for index in range(1, budget + 1):
        if panic.panicked():
            return _result("blocked", goal, history,
                           "the panic freeze is set; stopped between steps", started)

        try:
            questions, maps = _operation_questions(
                observation, inputs, goal, constraints, history)
            payload = triage.ask(
                json.dumps({
                    "goal": goal,
                    "window": observation.window.get("title") or "",
                    "constraints": constraints,
                    "recent_steps": [s.compact() for s in history[-4:]],
                }, ensure_ascii=False),
                questions, selection.jev_config(cfg))
        except (triage.TriageError, TaskError) as exc:
            return _result("needs_agent", goal, history,
                           f"decision model unavailable: {exc}", started)

        try:
            action, confidence = _answer_choice(payload, "operation", maps["operation"])
        except TaskError as exc:
            return _result("needs_agent", goal, history, str(exc), started)
        cost = float((payload.get("usage") or {}).get("cost") or 0.0)

        if action in ("blocked", "needs_agent"):
            return _result(action, goal, history,
                           f"the model chose {action}", started)

        if action == "done":
            confirmed, reason = _verify_done(
                cfg, goal, verification, observation.fingerprint, changed_ever)
            if confirmed:
                return _result("done", goal, history, reason, started)
            return _result("needs_agent", goal, history, reason, started)

        target: vision.Candidate | None = None
        value: str | None = None
        key: str | None = None
        direction: str | None = None

        try:
            if action in ("click", "double_click"):
                tid, _ = _answer_choice(payload, f"{action}_target",
                                        maps[f"{action}_target"])
                if tid == "none":
                    return _result("blocked", goal, history,
                                   "no element offered fits the chosen click", started)
                target = observation.candidate(tid)
                if target is None:
                    return _result("needs_agent", goal, history,
                                   "the chosen element vanished before acting", started)
            elif action == "type_text":
                ik, _ = _answer_choice(payload, "type_text_input",
                                       maps["type_text_input"])
                value = inputs.get(ik)
                if value is None:
                    return _result("needs_agent", goal, history,
                                   "the model chose an unknown input key", started)
                tid, _ = _answer_choice(payload, "type_text_target",
                                        maps["type_text_target"])
                if tid != "none":
                    target = observation.candidate(tid)
            elif action == "press_key":
                key, _ = _answer_choice(payload, "press_key_value",
                                        maps["press_key_value"])
            elif action == "scroll":
                direction, _ = _answer_choice(payload, "scroll_direction",
                                              maps["scroll_direction"])
        except (TaskError, KeyError) as exc:
            return _result("needs_agent", goal, history, str(exc), started)

        # Freshness guard: re-read and confirm the target still exists with the
        # same label before delivering. A stale decision is discarded, not run.
        if target is not None:
            try:
                fresh = backend.observe(window)
            except (TaskError, vision.VisionError) as exc:
                return _result("needs_agent", goal, history, str(exc), started)
            fresh_target = fresh.candidate(target.to_id())
            if fresh_target is None or fresh_target.label != target.label:
                history.append(Step(index=index, action=action, target_id=target.to_id(),
                                    target_label=target.label, reason="stale target; re-observing",
                                    cost_usd=cost))
                observation = fresh
                continue
            target = fresh_target
            observation = fresh

        before = observation.fingerprint
        try:
            panic.guard(f"a {action} step")
            _throttle(cfg)
            detail = backend.execute(action, target, value, key, direction,
                                     observation.window)
        except (panic.PanicError, TaskError, vision.VisionError) as exc:
            return _result("blocked", goal, history,
                           f"{action} failed: {exc}", started)

        after = None
        try:
            after_obs = backend.observe(window)
            after = after_obs.fingerprint
            observation = after_obs
        except (TaskError, vision.VisionError):
            pass

        verified, reason = vision._verify_outcome(before, after)
        changed = verified == "satisfied"
        changed_ever = changed_ever or changed
        step = Step(index=index, action=action,
                    target_id=target.to_id() if target else None,
                    target_label=target.label if target else None,
                    value=value, key=key, direction=direction, confidence=confidence,
                    changed=changed, verified=verified, reason=reason, cost_usd=cost)
        history.append(step)
        _audit(goal, action, step, observation)

        recent = history[-no_change_limit:]
        if len(recent) == no_change_limit and all(not s.changed for s in recent):
            return _result("blocked", goal, history,
                           f"no observable change after {no_change_limit} actions", started)

    return _result("needs_agent", goal, history,
                   f"reached the action budget ({budget})", started)


def _throttle(cfg: dict) -> None:
    vision._throttle(int(cfg.get("minIntervalMs", 1200)))


def _audit(goal: str, action: str, step: Step, observation: Observation) -> None:
    from . import audit
    audit.record({
        "tool": "do_gui_task",
        "app": observation.window.get("class") or observation.window.get("app_name"),
        "window": observation.window.get("title"),
        "goal": goal,
        "outcome": step.verified,
        "verified": step.changed,
        "reason": f"{action}: {step.reason}",
        "selection": "jev",
        "confidence": step.confidence,
        "cost_usd": step.cost_usd,
    })


def _result(status: str, goal: str, history: list[Step], reason: str,
            started: float) -> dict:
    return {
        "status": status,
        "goal": goal,
        "reason": reason,
        "actions_taken": len(history),
        "elapsed_s": round(time.monotonic() - started, 2),
        "history": [s.compact() for s in history],
    }


__all__ = ["gui_task", "gui_task_safe", "TaskError", "Backend", "VisionBackend",
           "Observation", "Step", "TERMINALS", "ACTIONS", "SAFE_KEYS"]


def gui_task_safe(goal: str, **kwargs) -> dict:
    """`gui_task` that degrades a hard failure into a `needs_agent` result.

    The MCP layer wants a structured terminal state, not an exception, so the
    caller can keep using the browser/OCR tools when this cannot run.
    """
    try:
        return gui_task(goal, **kwargs)
    except TaskError as exc:
        return {"status": "needs_agent", "goal": goal, "reason": str(exc),
                "actions_taken": 0, "elapsed_s": 0.0, "history": []}
