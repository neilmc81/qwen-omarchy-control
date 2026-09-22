"""Spoken outcome (#3): answer "did it work?" about the last GUI action.

Jev is a decision model: it returns typed answers, not prose. So this asks one
bounded Choice question ("is the stated goal now satisfied by the window?") and
**code composes the sentence**, exactly the way triage owns its policy wording.
That keeps the output reliable and language-stable, and it means the tool returns
a plain `spoken` string the voice frontend reads aloud — this module never calls
TTS or a desktop notification, so it survives a voice-agent swap.

It is read-only: it reads the window and the audit log, never clicks.
"""

from __future__ import annotations

import json

from . import audit, selection, triage, vision

# The typed outcomes Jev may return, mapped to the sentence code will speak.
OUTCOMES = {
    "done": "Yes - that looks done.",
    "partial": "It looks partly done.",
    "failed": "No, it doesn't look like it worked.",
    "unclear": "I can't tell from this window whether it worked.",
}


class OutcomeError(RuntimeError):
    """The outcome could not be judged."""


def _last_goal(app: str | None) -> str | None:
    """The most recent recorded goal, optionally for one app."""
    rows = audit.read_log(limit=20)
    for row in reversed(rows):
        goal = row.get("goal")
        if not goal:
            continue
        if app and app.lower() not in str(row.get("app") or "").lower():
            continue
        return str(goal)
    return None


def describe_outcome(goal: str | None = None, window: str | None = None,
                     cfg: dict | None = None) -> dict:
    """Judge whether `goal` (or the last recorded goal) is satisfied now.

    Returns `{status, spoken, goal, window, confidence}`. `status` is one of
    OUTCOMES plus `unknown` when nothing could be judged.
    """
    cfg = cfg or vision.load_config()
    ok, why = vision.available(cfg)
    if not ok:
        raise OutcomeError(why)

    resolved_goal = (goal or "").strip() or _last_goal(None)
    if not resolved_goal:
        return {
            "status": "unknown",
            "spoken": "I don't have a recent action to check.",
            "goal": None,
            "window": None,
            "confidence": 0.0,
        }

    win = vision.resolve_window(cfg, window)
    pid, window_id = win.get("pid"), win.get("window_id")
    tree = vision._run_driver(cfg, "get_window_state", {
        "pid": pid, "window_id": window_id, "include_screenshot": False,
    })
    if tree.get("degraded"):
        return {
            "status": "unknown",
            "spoken": ("I can't check that - this window has no readable "
                       "controls."),
            "goal": resolved_goal,
            "window": win.get("title"),
            "confidence": 0.0,
        }

    labels = [str(e.get("label") or e.get("name") or "")
              for e in (tree.get("elements") or [])]
    labels = [label for label in labels if label][:80]
    state = json.dumps({
        "goal": resolved_goal,
        "window_title": win.get("title"),
        "visible_controls": labels,
    }, ensure_ascii=False)

    criteria = {
        "done": "The window clearly shows the goal was accomplished.",
        "partial": "Some progress toward the goal is visible, but not all of it.",
        "failed": "The window shows the goal was not accomplished.",
        "unclear": "The window does not contain enough information to tell.",
    }
    questions = {
        "outcome": {
            "type": "choice",
            "instructions": (
                "A desktop agent was asked to accomplish the stated goal. Based "
                "only on the window's current controls, which best describes the "
                "result now?"
            ),
            "criteria": criteria,
        }
    }
    try:
        payload = triage.ask(state, questions, selection.jev_config(cfg))
    except triage.TriageError as exc:
        raise OutcomeError(f"outcome model unavailable: {exc}")

    answer = (payload.get("answers") or {}).get("outcome") or {}
    choice = str(answer.get("choice") or "unclear")
    confidence = float(answer.get("confidence") or 0.0)
    if choice not in OUTCOMES:
        choice = "unclear"

    return {
        "status": choice,
        "spoken": OUTCOMES[choice],
        "goal": resolved_goal,
        "window": win.get("title"),
        "confidence": round(confidence, 3),
    }


__all__ = ["describe_outcome", "OutcomeError", "OUTCOMES"]
