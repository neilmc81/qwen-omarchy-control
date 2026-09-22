"""Record once, replay by name (#5): a macro system for desktop chores.

Why record at *our* boundary
----------------------------
cua-driver has `start_recording`/`replay_trajectory`, but they capture *cua* tool
calls, and this project delivers input with ydotool/wtype (measured: cua's own
click is broken on Hyprland). A cua recording would replay the wrong mechanism.
So a macro records the same semantic step vocabulary `gui_task` already emits:

    (window identity, action, target role + label, value, key, direction)

Recording the *intent* ("click the Save button") rather than raw coordinates means
replay re-targets the live element, so a macro survives a window that moved its
button. Where the element is gone, replay reports the exact step it failed at.

Replay is deterministic, not model-driven: the actions are already known, so
there is no Jev call and no reasoning to go wrong. It is still verified - each
step is diffed before/after, and the run stops at the first step whose target is
missing or whose change cannot be observed, naming that step.

Storage: `~/.local/state/qwen-omarchy-control/macros/<name>.json` (0600).
Voice-agent agnostic: pure file + structured result, no audio or frontend calls.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

from . import panic, task, vision

STATE_HOME = Path(
    os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
)
MACROS_DIR = Path(os.environ.get(
    "QWEN_MACROS_DIR", STATE_HOME / "qwen-omarchy-control" / "macros"
))

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,63}$")

# The active recording, if any. Module state is fine: the MCP server is one
# long-lived process per voice session, like the click throttle in vision.py.
_active: dict | None = None


class MacroError(RuntimeError):
    """The macro could not be recorded, replayed, or found."""


def _check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise MacroError(
            "macro name must be 1-64 chars of letters, digits, spaces, - or _"
        )
    return name


def _path(name: str) -> Path:
    return MACROS_DIR / f"{name}.json"


def start(name: str) -> dict:
    """Begin recording steps under `name`."""
    global _active
    name = _check_name(name)
    if _active is not None:
        raise MacroError(
            f"already recording '{_active['name']}'; stop it first")
    _active = {
        "name": name,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "window": None,
        "steps": [],
    }
    return {"recording": name, "steps": 0}


def record_step(window: dict, action: str, target: vision.Candidate | None,
                value: str | None, key: str | None,
                direction: str | None) -> None:
    """Append one semantic step. Called by the gui_task loop while recording."""
    if _active is None:
        return
    if _active["window"] is None:
        _active["window"] = {
            "title": window.get("title"),
            "app_name": window.get("app_name") or window.get("class"),
        }
    _active["steps"].append({
        "action": action,
        "role": target.role if target else None,
        "label": target.label if target else None,
        "value": value,
        "key": key,
        "direction": direction,
    })


def is_recording() -> bool:
    return _active is not None


def stop() -> dict:
    """Finish recording and persist the macro. No steps -> refused."""
    global _active
    if _active is None:
        raise MacroError("no recording in progress")
    recording = _active
    _active = None
    if not recording["steps"]:
        raise MacroError("nothing was recorded; no macro was saved")
    MACROS_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "version": 1,
        "name": recording["name"],
        "created": recording["created"],
        "window": recording["window"],
        "steps": recording["steps"],
    }
    path = _path(recording["name"])
    fd, tmp = tempfile.mkstemp(prefix=".macro.", dir=MACROS_DIR)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return {"saved": recording["name"], "steps": len(recording["steps"])}


def load(name: str) -> dict:
    name = _check_name(name)
    try:
        return json.loads(_path(name).read_text())
    except FileNotFoundError:
        raise MacroError(f"no macro named '{name}'")
    except ValueError:
        raise MacroError(f"macro '{name}' is corrupt")


def list_macros() -> list[dict]:
    if not MACROS_DIR.exists():
        return []
    out: list[dict] = []
    for path in sorted(MACROS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            out.append({"name": data.get("name"), "steps": len(data.get("steps") or []),
                        "created": data.get("created")})
        except (OSError, ValueError):
            continue
    return out


def delete(name: str) -> dict:
    name = _check_name(name)
    try:
        _path(name).unlink()
        return {"deleted": name}
    except FileNotFoundError:
        raise MacroError(f"no macro named '{name}'")


def _match_candidate(observation: task.Observation, role: str | None,
                     label: str | None) -> vision.Candidate | None:
    """Find the live element matching a recorded (role, label).

    Exact label first, then a contains-match, preferring the recorded role. A
    replay must not silently click the wrong control, so a role mismatch is only
    accepted when the exact label is unambiguous.
    """
    if not label:
        return None
    exact = [c for c in observation.candidates if c.label == label]
    if role:
        exact_role = [c for c in exact if c.role == role]
        if exact_role:
            return exact_role[0]
    if exact:
        return exact[0]
    contains = [c for c in observation.candidates if label in (c.label or "")]
    if role:
        contains_role = [c for c in contains if c.role == role]
        if contains_role:
            return contains_role[0]
    return None if len(contains) != 1 else contains[0]


def replay(name: str, cfg: dict | None = None,
           _backend: task.Backend | None = None) -> dict:
    """Re-run a saved macro deterministically, verified step by step."""
    macro = load(name)
    cfg = cfg or vision.load_config()
    ok, why = vision.available(cfg)
    if not ok:
        raise MacroError(why)
    backend = _backend or task.VisionBackend(cfg)

    history: list[dict] = []
    started = time.monotonic()
    window_hint = (macro.get("window") or {}).get("title")

    try:
        observation = backend.observe(window_hint)
    except (task.TaskError, vision.VisionError) as exc:
        return _replay_result(name, "needs_agent", history,
                              f"could not observe the window: {exc}", started)
    if observation.degraded:
        return _replay_result(name, "needs_agent", history,
                              observation.degraded, started)

    for index, step in enumerate(macro["steps"], start=1):
        if panic.panicked():
            return _replay_result(name, "blocked", history,
                                  "the panic freeze is set", started)
        action = step.get("action")
        if action in ("done", "blocked", "needs_agent"):
            continue
        target = _match_candidate(observation, step.get("role"), step.get("label"))
        if action in ("click", "double_click", "type_text") and step.get("label") and target is None:
            return _replay_result(
                name, "blocked", history,
                f"step {index}: could not find '{step.get('label')}' on screen",
                started)
        before = observation.fingerprint
        try:
            panic.guard(f"a replay step")
            vision._throttle(int(cfg.get("minIntervalMs", 1200)))
            backend.execute(action, target, step.get("value"), step.get("key"),
                            step.get("direction"), observation.window)
        except (panic.PanicError, task.TaskError, vision.VisionError) as exc:
            return _replay_result(name, "blocked", history,
                                  f"step {index} ({action}) failed: {exc}", started)
        after = None
        try:
            observation = backend.observe(window_hint)
            after = observation.fingerprint
        except (task.TaskError, vision.VisionError):
            pass
        verified, reason = vision._verify_outcome(before, after)
        history.append({
            "step": index, "action": action,
            "target": step.get("label") or step.get("value"),
            "verified": verified, "reason": reason,
        })
        if verified == "unsatisfied":
            return _replay_result(
                name, "blocked", history,
                f"step {index} produced no observable change; stopped rather "
                "than continue blindly", started)

    return _replay_result(name, "done", history,
                          f"replayed {len(history)} step(s)", started)


def _replay_result(name: str, status: str, history: list[dict], reason: str,
                   started: float) -> dict:
    spoken = {
        "done": f"Replayed '{name}' - all {len(history)} steps verified.",
        "blocked": f"I couldn't finish replaying '{name}': {reason}",
        "needs_agent": f"I couldn't replay '{name}': {reason}",
    }.get(status, reason)
    return {
        "status": status,
        "macro": name,
        "spoken": spoken,
        "reason": reason,
        "steps_run": len(history),
        "elapsed_s": round(time.monotonic() - started, 2),
        "history": history,
    }


__all__ = ["start", "stop", "record_step", "is_recording", "replay", "load",
           "list_macros", "delete", "MacroError", "MACROS_DIR"]
