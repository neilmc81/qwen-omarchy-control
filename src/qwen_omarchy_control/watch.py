"""Watch-and-narrate (#7): speak when a window condition flips.

"Watch that window and tell me when the export finishes."

This is #1b's pattern on a timer: poll a window's accessibility tree on a short
interval and ask Jev one `noul` question ("is the export finished?") against it.
When the reading crosses `threshold`, the job completes with status `met`.

It is **read-only**: it never clicks or types, so it is allowed while the panic
freeze is set (like `find_element`). It runs as a pollable background job (see
`jobs.py`) rather than a blocking call, so the voice frontend can ask "done yet?"
without holding a call open. A desktop notification is an optional courtesy; the
job state file is the mechanism. That keeps it voice-agent agnostic.
"""

from __future__ import annotations

import json
import time

from . import jobs, selection, triage, vision

DEFAULT_INTERVAL_S = 5.0
DEFAULT_TIMEOUT_S = 900.0
MIN_INTERVAL_S = 2.0


class WatchError(RuntimeError):
    """The watch could not be started or found."""


def _evaluate(cfg: dict, window: dict, condition: str) -> float:
    """One reading: probability the condition is true for the window now."""
    pid, window_id = window.get("pid"), window.get("window_id")
    tree = vision._run_driver(cfg, "get_window_state", {
        "pid": pid, "window_id": window_id, "include_screenshot": False,
    })
    if tree.get("degraded"):
        return 0.0
    labels = [str(e.get("label") or e.get("name") or "")
              for e in (tree.get("elements") or [])]
    labels = [label for label in labels if label][:80]
    state = json.dumps({
        "condition": condition,
        "window_title": tree.get("window_title"),
        "visible_controls": labels,
    }, ensure_ascii=False)
    questions = {
        "met": {
            "type": "noul",
            "instructions": (
                "Based only on the window's current visible controls, is the "
                "following condition true now? " + condition
            ),
        }
    }
    payload = triage.ask(state, questions, selection.jev_config(cfg))
    return float((payload.get("answers") or {}).get("met", {}).get("noul") or 0.0)


def watch_start(window: str | None, condition: str,
                interval_s: float = DEFAULT_INTERVAL_S,
                timeout_s: float = DEFAULT_TIMEOUT_S,
                threshold: float = 0.7,
                cfg: dict | None = None) -> dict:
    """Start watching; return the job id immediately."""
    condition = (condition or "").strip()
    if not condition:
        raise WatchError("a condition is required")
    cfg = cfg or vision.load_config()
    ok, why = vision.available(cfg)
    if not ok:
        raise WatchError(why)
    interval = max(MIN_INTERVAL_S, float(interval_s))
    timeout = max(interval, float(timeout_s))
    resolved = vision.resolve_window(cfg, window)

    def worker(job_id: str, stop) -> None:
        started = time.monotonic()
        while not stop.is_set():
            if time.monotonic() - started >= timeout:
                jobs.finish(job_id, "timeout",
                            spoken=f"Still not seeing: {condition}")
                return
            try:
                probability = _evaluate(cfg, resolved, condition)
            except (triage.TriageError, vision.VisionError) as exc:
                jobs.update(job_id, note=f"reading failed: {exc}")
                probability = 0.0
            if probability >= threshold:
                jobs.finish(job_id, "met", probability=round(probability, 3),
                            spoken=f"Done - {condition}")
                return
            jobs.update(job_id, probability=round(probability, 3))
            stop.wait(interval)
        jobs.finish(job_id, "stopped", spoken=f"Stopped watching: {condition}")

    job_id = jobs.start("watch", worker, window=resolved.get("title"),
                        condition=condition, threshold=threshold)
    return {"job_id": job_id, "window": resolved.get("title"),
            "condition": condition, "status": "running",
            "spoken": f"Watching {resolved.get('title') or 'the window'}."}


def watch_check(job_id: str) -> dict:
    record = jobs.read(job_id)
    if record is None:
        raise WatchError(f"no job {job_id!r}")
    return record


def watch_stop(job_id: str) -> dict:
    if not jobs.stop(job_id):
        record = jobs.read(job_id)
        if record is None:
            raise WatchError(f"no job {job_id!r}")
        return record
    # Wait briefly for the worker to write its terminal state.
    for _ in range(20):
        record = jobs.read(job_id)
        if record and record.get("status") in jobs.TERMINAL:
            return record
        time.sleep(0.05)
    return jobs.read(job_id) or {"id": job_id, "status": "stopping"}


__all__ = ["watch_start", "watch_check", "watch_stop", "WatchError"]
