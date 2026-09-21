"""Trajectory audit: record every GUI action and whether it was verified.

Why this exists
---------------
`click_element` already decides, per action, whether the outcome was observable
(`satisfied` / `unsatisfied` / `unknown`). Until now that judgement was returned
to the caller and thrown away. Without a record there is no way to answer the
question the whole computer-use effort depends on: *does this actually work?*

This is the "measure before trust" rule - the same discipline that caught the
triage false-positives in log mode - applied to GUI actions. It is log-only: it
never changes behaviour, never gates an action, and never raises into the action
path. A write failure is swallowed.

What a record holds
-------------------
One NDJSON object per action, appended to
`$XDG_STATE_HOME/qwen-omarchy-control/trajectory.jsonl` (mode 0600). Fields are
chosen so `summarize` can compute a success rate per app without reading the
whole file back into memory:

    ts, tool, app, window, goal, outcome, verified, reason, attempts, latency_ms,
    cost_usd, double, takeover

`unsatisfied` is deliberately NOT counted as a failure in the summary. Measured
on this machine, a single click on a Nautilus grid cell selects it but the tree
does not expose the selection, so verification correctly reports "no observable
change". Counting that as a click failure would libel a working action. The
summary therefore reports three bands - `satisfied`, `unsatisfied`, `unknown` -
and lets the reader judge, alongside a `verifiable_rate` that says how often the
app exposes enough state for success to be provable at all.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from . import triage

STATE_HOME = Path(
    os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
)

# Overridable so tests never touch the live log.
LOG_FILE = Path(os.environ.get(
    "QWEN_TRAJECTORY_LOG", STATE_HOME / "qwen-omarchy-control" / "trajectory.jsonl"
))

# The fields a record may carry; anything else is dropped before writing so a
# wayward caller cannot stuff a credential or a screenshot into the log.
_FIELDS = (
    "ts", "tool", "app", "window", "goal", "outcome", "verified", "reason",
    "attempts", "latency_ms", "cost_usd", "double", "takeover",
    # How a target was chosen: "jev" (the System One model) or "fallback" (the
    # deterministic scorer used when the model is unavailable), with the model's
    # confidence. Lets the success rate be split by selection method.
    "selection", "confidence",
)


def record(action: dict) -> dict | None:
    """Append one action record. Returns the written entry, or None on failure.

    Never raises: an audit write must not be able to fail an action that already
    happened. The caller is the only authority on whether the action succeeded.
    """
    try:
        entry = {
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
            **{key: action.get(key) for key in _FIELDS if key != "ts"},
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write(line + "\n")
        return entry
    except (OSError, TypeError, ValueError):
        return None


def log_click(result: dict, tool: str = "click_element") -> dict | None:
    """Record a `vision.click_element` result in the standard shape.

    `confidence` and `selection` come from the element chooser (`find_element`
    returns them) and are what make the log calibratable: without them there is
    no way to ask "was the floor too low for the calls that failed?".
    """
    return record({
        "tool": tool,
        "app": result.get("window_class") or result.get("window_title"),
        "window": result.get("window_title"),
        "goal": result.get("goal"),
        "outcome": result.get("verification"),
        "verified": result.get("verified"),
        "reason": result.get("verification_reason"),
        "attempts": result.get("attempts"),
        "latency_ms": int(round(float(result.get("elapsed_s") or 0.0) * 1000)),
        "cost_usd": result.get("cost_usd"),
        "double": result.get("double"),
        "takeover": result.get("takeover"),
        "selection": result.get("selection") or ("jev" if result.get("confidence") is not None else None),
        "confidence": result.get("confidence"),
    })


def read_log(limit: int = 200) -> list[dict]:
    if not LOG_FILE.exists():
        return []
    rows: list[dict] = []
    for line in LOG_FILE.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows[-limit:]


def _per_app(rows: list[dict]) -> dict[str, dict]:
    apps: dict[str, dict] = {}
    for row in rows:
        app = str(row.get("app") or "unknown")
        bucket = apps.setdefault(app, {
            "actions": 0, "satisfied": 0, "unsatisfied": 0, "unknown": 0,
            "cost_usd": 0.0,
        })
        bucket["actions"] += 1
        outcome = str(row.get("outcome") or "unknown")
        if outcome in ("satisfied", "unsatisfied", "unknown"):
            bucket[outcome] += 1
        else:
            bucket["unknown"] += 1
        bucket["cost_usd"] = round(
            bucket["cost_usd"] + float(row.get("cost_usd") or 0.0), 6)
    for bucket in apps.values():
        verifiable = bucket["satisfied"] + bucket["unsatisfied"]
        bucket["verifiable_rate"] = (
            round(verifiable / bucket["actions"], 3) if bucket["actions"] else 0.0)
        bucket["success_rate_of_verifiable"] = (
            round(bucket["satisfied"] / verifiable, 3) if verifiable else None)
    return apps


def summarize(rows: list[dict]) -> dict:
    """Counts that answer 'is this reliable enough to trust?' per app."""
    satisfied = unsatisfied = unknown = 0
    cost = 0.0
    attempts = 0
    for row in rows:
        outcome = str(row.get("outcome") or "unknown")
        if outcome == "satisfied":
            satisfied += 1
        elif outcome == "unsatisfied":
            unsatisfied += 1
        else:
            unknown += 1
        cost += float(row.get("cost_usd") or 0.0)
        attempts += int(row.get("attempts") or 1)
    verifiable = satisfied + unsatisfied
    return {
        "actions": len(rows),
        "satisfied": satisfied,
        "unsatisfied": unsatisfied,
        "unknown": unknown,
        "verifiable_rate": round(verifiable / len(rows), 3) if rows else 0.0,
        "success_rate_of_verifiable": (
            round(satisfied / verifiable, 3) if verifiable else None),
        "retries": max(0, attempts - len(rows)),
        "cost_usd": round(cost, 6),
        "by_app": _per_app(rows),
    }


__all__ = ["record", "log_click", "read_log", "summarize", "LOG_FILE"]
