"""Multi-step in one breath (#6): an ordered list of verified GUI steps.

Why this exists
---------------
`do_gui_task` runs one bounded, verified loop. A spoken request like "open
Documents, make a folder called Taxes, and move the newest PDF there" is several
such loops in a row. Chaining them in the voice agent is the wrong place: the
frontend would have to reason about partial failure, and every one of those
choices is voice-agent-specific.

So the sequence lives here. It runs an ordered list of `gui_task` steps, each
with its own goal and verification, and **stops at the first step that cannot be
verified** rather than pressing on. The result names the exact step that failed,
so the spoken answer is honest ("I got as far as making the folder; moving the
file failed").

Voice-agent agnostic by construction: the return value carries a `spoken` string
and structured per-step results. No TTS, no notification, no Qwen dependency.
"""

from __future__ import annotations

import time

from . import task


class SequenceError(RuntimeError):
    """The sequence could not be attempted (bad arguments)."""


def _spoken_summary(goal: str, results: list[dict], steps_total: int,
                    status: str, failed_label: str | None) -> str:
    done = [r for r in results if r.get("status") == "done"]
    if status == "done":
        return f"Done. All {steps_total} steps of '{goal}' completed."
    if failed_label:
        return (f"I got through {len(done)} of {steps_total} steps of "
                f"'{goal}', then stopped at: {failed_label}.")
    return f"I couldn't finish '{goal}' ({status})."


def do_sequence(goal: str, steps: list[dict], window: str | None = None,
                inputs: dict[str, str] | None = None,
                max_actions_per_step: int | None = None,
                cfg: dict | None = None,
                _runner=None) -> dict:
    """Run ordered `steps` (each with `goal` + optional `verification`).

    Stops at the first non-`done` step. `_runner` is an injection point for
    tests; it defaults to `task.gui_task`.
    """
    goal = (goal or "").strip()
    if not goal:
        raise SequenceError("a sequence goal is required")
    if not steps:
        raise SequenceError("at least one step is required")
    if len(steps) > 12:
        raise SequenceError("too many steps (max 12)")

    normalized: list[dict] = []
    for raw in steps:
        step_goal = str((raw or {}).get("goal") or "").strip()
        if not step_goal:
            raise SequenceError("every step needs a goal")
        normalized.append({
            "goal": step_goal,
            "verification": [str(v) for v in (raw.get("verification") or [])],
            "inputs": {**{str(k): str(v) for k, v in (inputs or {}).items()},
                       **{str(k): str(v) for k, v in (raw.get("inputs") or {}).items()}},
            "window": str(raw.get("window") or window) if (raw.get("window") or window) else None,
        })

    runner = _runner or task.gui_task
    started = time.monotonic()
    results: list[dict] = []
    status = "done"
    failed_label: str | None = None

    for index, step in enumerate(normalized, start=1):
        try:
            result = runner(
                step["goal"],
                window=step["window"],
                inputs=step["inputs"],
                verification=step["verification"],
                max_actions=max_actions_per_step,
                cfg=cfg,
            )
        except task.TaskError as exc:
            result = {"status": "needs_agent", "goal": step["goal"],
                      "reason": str(exc), "actions_taken": 0, "history": []}
        results.append({"step": index, **result})
        if result.get("status") != "done":
            status = result.get("status", "needs_agent")
            failed_label = step["goal"]
            break

    return {
        "status": status,
        "goal": goal,
        "spoken": _spoken_summary(goal, results, len(normalized), status,
                                  failed_label),
        "steps_completed": sum(1 for r in results if r.get("status") == "done"),
        "steps_total": len(normalized),
        "failed_step": failed_label,
        "elapsed_s": round(time.monotonic() - started, 2),
        "results": results,
    }


__all__ = ["do_sequence", "SequenceError"]
