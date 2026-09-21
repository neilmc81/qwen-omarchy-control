"""Precise GUI element targeting via Cua's accessibility tree + Jev selection.

Why this exists
---------------
The OCR tools (`read_window`, `read_screen`, `mouse_click`, `type_text`) guess at
pixels: tesseract reads a picture, and a click lands on a coordinate. The
project's own README admits OCR "can garble" small or low-contrast text. But the
desktop already exposes a structured accessibility (AT-SPI) tree through
`cua-driver`: every actionable element with a role, a label, bounds and an opaque
handle. There is no need to guess.

What this does
--------------
For a goal like "click the Documents folder", it is a bounded selection, not a
GUI-planning loop:

    cua-driver get_window_state   -> candidates (role, label, index, token)
    Jev (System One)              -> pick exactly one candidate id, with confidence
    code                          -> click that element's token via cua-driver

The model never invents a coordinate and never picks an id that was not supplied
(the same contract Cua's own `jev-use` recipe uses). Code owns the action.

Why it is dormant and off the voice path
----------------------------------------
This hangs off the side of the voice loop exactly like the pre-dispatch triage:
it is a *tool the frontend may call*, never something between the microphone and
the model. It ships disabled. The existing OCR tools are untouched and remain the
default; this is opt-in per call and falls back to them when unavailable.

Everything degrades to "unavailable" rather than raising into the voice path:
no cua-driver, no key, network failure, or a degraded (non-AT-SPI) tree all make
the tool report that it cannot help, and the caller keeps its current behaviour.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import panic, selection, triage

# --- config ---------------------------------------------------------------

DEFAULT_CONFIG = {
    # Kill switch, default ON. The tools are meant to be used by the agent when
    # a request calls for them, with no manual enable step: they self-degrade to
    # a clear "unavailable" message when cua-driver, the API key, or the
    # window's accessibility tree is missing, and the caller keeps the OCR tools.
    # Set false only to remove the capability entirely.
    "enabled": True,
    # Announce the input takeover (desktop notification) before clicking, so the
    # user knows not to touch the mouse or keyboard while the agent drives.
    "announceTakeover": True,
    # Minimum Jev confidence to act on a chosen element.
    "minConfidence": 0.60,
    # Cap the candidate list so a huge tree cannot blow the request budget.
    "maxCandidates": 40,
    "timeoutMs": 12000,
    "driverTimeoutMs": 20000,
    "model": triage.DEFAULT_MODEL,
    "endpoint": triage.DEFAULT_ENDPOINT,
    "apiKeyEnv": "OPENROUTER_API_KEY",
    "driver": "cua-driver",
    # cua-driver 0.28.2 needs its Wayland backend enabled on Hyprland.
    "enableWayland": True,
    # Verify the outcome of a click so the result is reported honestly.
    "verify": True,
    "verifyDelayMs": 600,
    # Minimum spacing between delivered clicks, in ms. This is a stability
    # guard, not politeness: measured on this machine, GTK4/Nautilus segfaults
    # in its GSK renderer (gsk_renderer_render) when folders are opened in rapid
    # programmatic succession. An agent should not machine-gun the UI anyway, so
    # input is throttled to a human-ish pace.
    "minIntervalMs": 1200,
    # Automatic retries default to 0, and that is a safety decision, not a
    # placeholder. A retry is a SECOND click: on a single click that becomes a
    # double-click (which navigates or opens), and on a button it can submit
    # twice. Measured live: clicking a folder selects it, but grid-cell
    # selection is not exposed in the tree, so verification correctly reports
    # "no observable change" and a retry would have silently double-clicked.
    # Raise this only for a target known to be idempotent.
    "retries": 0,
    # #1b: when the state diff sees no change, ask Jev a yes/no question over the
    # before/after trees to catch a real-but-subtle success (e.g. a value changed
    # inside an unlabelled field). Dormant by default, per the measure-first rule:
    # turn it on once the audit log shows how often the unsatisfied branch is hit.
    "jevOutcome": False,
    "jevOutcomeThreshold": 0.70,
}

CONFIG_HOME = triage.CONFIG_HOME
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")

# Panic stop: while this file exists, no input is delivered. The hotkey
# (SUPER + SHIFT + ESCAPE) creates it; the agent checks it before every click.
PANIC_FILE = Path(os.environ.get(
    "QWEN_VISION_PANIC_FILE", RUNTIME_DIR / "qwen-voice" / "stop"
))

CONFIG_FILE = Path(os.environ.get(
    "QWEN_VISION_CONFIG", CONFIG_HOME / "qwen-omarchy-control" / "vision.json"
))

# Time of the last delivered click, for the minimum-interval throttle. Module
# state is fine here: the MCP server is one long-lived process per voice session.
_last_click_at = 0.0


def _throttle(min_interval_ms: int) -> float:
    """Sleep so clicks are spaced at least `min_interval_ms` apart.

    Returns the seconds waited. Guards against machine-gunning a GUI, which is
    both bad manners and, measured here, able to crash GTK4's renderer.
    """
    global _last_click_at
    interval = max(0.0, min_interval_ms / 1000.0)
    now = time.monotonic()
    waited = 0.0
    if _last_click_at and interval:
        remaining = interval - (now - _last_click_at)
        if remaining > 0:
            time.sleep(remaining)
            waited = remaining
    _last_click_at = time.monotonic()
    return waited


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return cfg
    if isinstance(data, dict):
        for key in DEFAULT_CONFIG:
            if data.get(key) is not None:
                cfg[key] = data[key]
    return cfg


# --- errors ---------------------------------------------------------------


class VisionError(RuntimeError):
    """Targeting could not be completed; the caller should use its fallback."""


@dataclass
class Candidate:
    index: int
    role: str
    label: str
    token: str = ""
    enabled: bool = True
    frame: dict = field(default_factory=dict)

    @property
    def description(self) -> str:
        bits = [self.role or "element"]
        if self.label:
            bits.append(f'"{self.label}"')
        if not self.enabled:
            bits.append("(disabled)")
        return " ".join(bits)

    def to_id(self) -> str:
        return f"e{self.index}"


# --- cua-driver -----------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _driver_env(cfg: dict) -> dict:
    env = os.environ.copy()
    if cfg.get("enableWayland"):
        env["CUA_DRIVER_RS_ENABLE_WAYLAND"] = "1"
    return env


def _run_driver(cfg: dict, tool: str, args: dict) -> dict:
    """Run one cua-driver tool call and parse its JSON.

    cua-driver is invoked as `cua-driver <tool> '<json>'`, matching the CLI
    contract in its own skill. Its stdout can carry ANSI, so the JSON object is
    located rather than assumed to be the whole stream.
    """
    driver = str(cfg.get("driver") or "cua-driver")
    timeout = max(1.0, float(cfg.get("driverTimeoutMs", 20000)) / 1000.0)
    try:
        proc = subprocess.run(
            [driver, tool, json.dumps(args)],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, env=_driver_env(cfg),
        )
    except FileNotFoundError:
        raise VisionError(f"{driver} is not installed")
    except subprocess.TimeoutExpired:
        raise VisionError(f"{tool} timed out")
    except OSError as exc:
        raise VisionError(f"{tool} could not run: {exc}")

    text = _ANSI_RE.sub("", (proc.stdout or "") + (proc.stderr or ""))
    payload = _extract_json(text)
    if payload is None:
        detail = text.strip().splitlines()[-1][:200] if text.strip() else "no output"
        raise VisionError(f"{tool} returned no JSON ({detail})")
    # A refusal is a normal, expected outcome; surface it for a clean fallback.
    if isinstance(payload, dict) and payload.get("refusal"):
        refusal = payload["refusal"]
        raise VisionError(
            f"{tool} refused: {refusal.get('code') or refusal}"
        )
    return payload


def _extract_json(text: str) -> dict | None:
    """First complete top-level JSON object in `text`, or None."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:i + 1])
                    except ValueError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    return None


def list_windows(cfg: dict, pid: int | None = None) -> list[dict]:
    args = {"pid": pid} if pid else {}
    payload = _run_driver(cfg, "list_windows", args)
    windows = payload.get("windows")
    return windows if isinstance(windows, list) else []


def _focused_window() -> dict | None:
    """The focused window per Hyprland, or None.

    cua-driver's window list does not carry a focus flag (measured: both
    `is_focused` and `focused` are absent), so "omit the window to use the
    focused one" cannot be served from its list. Hyprland knows the active
    window, and its pid/title match cua's entries, so it is used only to resolve
    the target and then discarded.
    """
    from .desktop import DesktopController
    try:
        active = DesktopController().get_active_window()
    except Exception:  # noqa: BLE001 - focus lookup must never break targeting
        return None
    return active if active.get("address") else None


def resolve_window(cfg: dict, hint: str | None) -> dict:
    """Pick a window by pid, by matching a title/name substring, or the focused one."""
    if not hint:
        # The tools document "omit the window for the focused window". Honour it
        # rather than raising, so "what can I do here?" works with no argument.
        active = _focused_window()
        if not active:
            raise VisionError(
                "no target window given and none is focused; pass pid or a "
                "title/name"
            )
        pid = active.get("pid")
        if pid:
            try:
                windows = list_windows(cfg, int(pid))
                # Prefer the entry whose title matches the active one.
                title = str(active.get("title") or "").lower()
                for window in windows:
                    if title and title[:20] in str(window.get("title") or "").lower():
                        return window
                for window in windows:
                    if window.get("is_on_screen"):
                        return window
                if windows:
                    return windows[0]
            except VisionError:
                pass
        hint = str(active.get("class") or active.get("title") or "")
        if not hint:
            raise VisionError("the focused window could not be resolved")

    if hint and hint.isdigit():
        pid = int(hint)
        windows = list_windows(cfg, pid)
        if not windows:
            raise VisionError(f"pid {pid} has no windows")
        # Prefer an on-screen window.
        for window in windows:
            if window.get("is_on_screen"):
                return window
        return windows[0]

    windows = list_windows(cfg)
    if not windows:
        raise VisionError("no application windows are available")
    if not hint:
        raise VisionError("no target window; pass pid or a title/name")
    needle = hint.strip().lower()
    matches = [
        w for w in windows
        if needle in str(w.get("title") or "").lower()
        or needle in str(w.get("app_name") or w.get("name") or "").lower()
    ]
    if not matches:
        raise VisionError(f"no window matches {hint!r}")
    for window in matches:
        if window.get("is_on_screen"):
            return window
    return matches[0]


# --- candidates -----------------------------------------------------------


def _is_interactive(element: dict) -> bool:
    actions = element.get("actions") or []
    return bool(actions)


def candidates_from_tree(tree: dict, cfg: dict) -> tuple[list[Candidate], str]:
    """Actionable, labelled elements from a get_window_state response.

    Unlabelled and actionless structural nodes are dropped: a candidate list is
    only useful if every entry is something Jev could reasonably choose.
    """
    elements = tree.get("elements") or []
    snapshot_id = str(tree.get("snapshot_id") or "")
    out: list[Candidate] = []
    for element in elements:
        role = str(element.get("role") or "").strip()
        label = str(element.get("label") or element.get("name") or "").strip()
        # A window/root with no label is structure, not a target.
        if not label and role not in ("push button", "button", "menu item", "link"):
            continue
        if not _is_interactive(element) and role not in (
            "push button", "button", "menu item", "link", "check box",
            "radio button", "entry", "text", "grid cell", "list item",
        ):
            continue
        out.append(Candidate(
            index=int(element.get("element_index") or 0),
            role=role,
            label=label,
            token=str(element.get("element_token") or ""),
            enabled=bool(element.get("enabled", True)),
            frame=element.get("frame") or {},
        ))
    cap = int(cfg.get("maxCandidates", 40))
    truncated = len(out) > cap
    return out[:cap], snapshot_id


# --- Jev selection --------------------------------------------------------


def _jev_ask(cfg: dict, goal: str, window_title: str, candidates: list[Candidate],
              extra_questions: dict | None = None) -> dict:
    """One Jev fan-out call using the existing triage transport."""
    criteria = {c.to_id(): c.description for c in candidates}
    questions = {
        "candidate": {
            "type": "choice",
            "instructions": (
                f"Goal: {goal}. Select exactly one element id from the supplied "
                "list that best accomplishes the goal. If none fits, choose "
                "\"none\"."
            ),
            "criteria": {**criteria, "none": "No supplied element accomplishes the goal"},
        },
    }
    if extra_questions:
        questions.update(extra_questions)
    state = {
        "goal": goal,
        "window_title": window_title,
        "elements": [
            {"id": c.to_id(), "role": c.role, "label": c.label,
             "enabled": c.enabled}
            for c in candidates
        ],
    }
    try:
        return triage.ask(json.dumps(state, ensure_ascii=False), questions,
                          selection.jev_config(cfg))
    except triage.TriageError as exc:
        raise VisionError(f"selection model unavailable: {exc}")


def select(cfg: dict, goal: str, window: dict, candidates: list[Candidate]) -> dict:
    """Ask Jev for one candidate id; return the chosen Candidate + metadata."""
    if not candidates:
        raise VisionError("no actionable elements found in this window")
    title = str(window.get("title") or window.get("app_name") or "")
    payload = _jev_ask(cfg, goal, title, candidates)
    by_id = {c.to_id(): c for c in candidates}
    try:
        choice = selection.choose(payload, set(by_id), cfg)
    except selection.SelectionError as exc:
        raise VisionError(f"{exc}; use the OCR tools instead")
    candidate = by_id[choice.id]
    return {
        "candidate": candidate,
        "confidence": choice.confidence,
        "probabilities": choice.probabilities,
        "cost_usd": choice.cost_usd,
    }


def _ask_yes_no(cfg: dict, state: str, instructions: str) -> dict:
    """One yes/no Noul question over `state` via the same Jev transport."""
    questions = {"yes": {"type": "noul", "instructions": instructions}}
    try:
        return triage.ask(state, questions, selection.jev_config(cfg))
    except triage.TriageError as exc:
        raise VisionError(f"outcome model unavailable: {exc}")


def verify_outcome_jev(cfg: dict, goal: str, before: dict | None,
                       after: dict | None) -> tuple[str, str]:
    """Ask Jev whether the intended outcome happened, over the fresh tree.

    The state diff is the primary signal and is right most of the time. It is
    blind to a real-but-subtle change: a value that changed inside a field whose
    label did not, for instance. When the diff says nothing changed, this asks a
    single yes/no question over the before/after trees so a subtle success is not
    reported as a failure.

    It is a disambiguator, not a hot path: it runs only on the `unsatisfied`
    branch, and any failure (no key, timeout, malformed answer) degrades to the
    original `unsatisfied` verdict rather than inventing a success.
    """
    if not before or not after:
        return "unsatisfied", "nothing about the window changed"
    state = json.dumps({
        "goal": goal,
        "before": _tree_view(before),
        "after": _tree_view(after),
    }, ensure_ascii=False)
    try:
        payload = _ask_yes_no(
            cfg, state,
            "A desktop agent clicked an element to accomplish the stated goal. "
            "Comparing the window before and after the click, did the intended "
            "outcome happen, even if the change is subtle?",
        )
    except VisionError:
        return "unsatisfied", "nothing about the window changed"
    probability = float((payload.get("answers") or {}).get("yes", {}).get("noul") or 0.0)
    threshold = float(cfg.get("jevOutcomeThreshold", 0.7))
    if probability >= threshold:
        return (
            "satisfied",
            f"no visible change but the outcome model judged it done ({probability:.2f})",
        )
    return (
        "unsatisfied",
        f"nothing about the window changed (outcome model: {probability:.2f})",
    )


def _tree_view(fingerprint: dict) -> dict:
    """A compact, model-readable view of a fingerprint (labels only)."""
    labels = [entry[0] for entry in (fingerprint.get("elements") or []) if entry[0]]
    return {"title": fingerprint.get("title") or "", "labels": labels[:60]}


# --- public API -----------------------------------------------------------


def available(cfg: dict | None = None) -> tuple[bool, str]:
    """Whether precise targeting can be used right now, and why not if not."""
    cfg = cfg or load_config()
    if not cfg.get("enabled"):
        return False, "precise targeting is disabled (config enabled=false)"
    import shutil
    driver = str(cfg.get("driver") or "cua-driver")
    if not shutil.which(driver):
        return False, f"{driver} is not installed"
    try:
        triage._read_key(dict(triage.load_config(), apiKeyEnv=cfg.get("apiKeyEnv")))
    except triage.TriageError as exc:
        return False, str(exc)
    return True, ""


def find_element(goal: str, window_hint: str | None = None,
                 cfg: dict | None = None) -> dict:
    """Locate the element that satisfies `goal`. Raises VisionError otherwise.

    Read-only, so the panic flag does not block it: the agent may still look at
    the screen while stopped, it just may not act.
    """
    cfg = cfg or load_config()
    ok, reason = available(cfg)
    if not ok:
        raise VisionError(reason)
    if not (goal or "").strip():
        raise VisionError("a goal is required to choose an element")

    started = time.monotonic()
    window = resolve_window(cfg, window_hint)
    pid = window.get("pid")
    window_id = window.get("window_id")
    if pid is None or window_id is None:
        raise VisionError("resolved window has no pid/window_id")
    tree = _run_driver(cfg, "get_window_state", {
        "pid": pid, "window_id": window_id, "include_screenshot": False,
    })
    if tree.get("degraded"):
        raise VisionError(
            "this window has no accessibility tree (a canvas/Electron surface); "
            "use the OCR tools instead"
        )
    candidates, snapshot_id = candidates_from_tree(tree, cfg)
    result = select(cfg, goal, window, candidates)
    candidate = result["candidate"]
    return {
        "pid": pid,
        "window_id": window_id,
        "window_title": window.get("title"),
        "window_class": window.get("class") or window.get("app_name"),
        "snapshot_id": snapshot_id,
        "element_index": candidate.index,
        "element_token": candidate.token,
        "role": candidate.role,
        "label": candidate.label,
        "frame": candidate.frame,
        "confidence": round(result["confidence"], 3),
        "candidate_count": len(candidates),
        "cost_usd": result["cost_usd"],
        "elapsed_s": round(time.monotonic() - started, 2),
    }


# --- "what can I do here?" -------------------------------------------------


def _rank_actions(cfg: dict, window: dict, candidates: list[Candidate]) -> list[dict]:
    """Ask Jev which 3-5 candidates are the meaningful actions, in order.

    One choice question over the same candidate list `select` uses: the model
    picks the option that names the most useful actions rather than inventing
    prose. Code owns how many are kept and how they are presented.
    """
    criteria = {
        c.to_id(): c.description for c in candidates
    }
    criteria["none"] = "None of these is a meaningful action"
    questions = {
        "actions": {
            "type": "choice",
            "instructions": (
                f"Window: {window.get('title') or window.get('app_name') or 'unknown'}. "
                "Which of these elements are the meaningful things a user would "
                "recognise as the main actions here (open, save, send, delete, "
                "navigate)?"
            ),
            "criteria": criteria,
        },
    }
    jev_cfg = dict(triage.load_config())
    for key in ("model", "endpoint", "apiKeyEnv"):
        jev_cfg[key] = cfg.get(key) or jev_cfg.get(key)
    state = json.dumps({
        "window_title": str(window.get("title") or window.get("app_name") or ""),
        "elements": [
            {"id": c.to_id(), "role": c.role, "label": c.label,
             "enabled": c.enabled}
            for c in candidates
        ],
    }, ensure_ascii=False)
    return triage.ask(state, questions, jev_cfg)


def describe_actions(window_hint: str | None = None,
                     cfg: dict | None = None) -> dict:
    """Answer "what can I do here?" - read a window and name its main actions.

    Read-only, so it is allowed while the panic freeze is set: looking is always
    permitted even when acting is not. It never clicks and never moves the mouse.
    """
    cfg = cfg or load_config()
    ok, reason = available(cfg)
    if not ok:
        raise VisionError(reason)

    started = time.monotonic()
    window = resolve_window(cfg, window_hint)
    pid = window.get("pid")
    window_id = window.get("window_id")
    if pid is None or window_id is None:
        raise VisionError("resolved window has no pid/window_id")
    try:
        tree = _run_driver(cfg, "get_window_state", {
            "pid": pid, "window_id": window_id, "include_screenshot": False,
        })
    except VisionError as exc:
        raise VisionError(f"could not read this window: {exc}")
    if tree.get("degraded"):
        raise VisionError(
            "this window has no accessibility tree; use read_screen instead"
        )
    candidates, _ = candidates_from_tree(tree, cfg)
    if not candidates:
        raise VisionError("no labelled controls were found in this window")

    payload = _rank_actions(cfg, window, candidates)
    answers = payload.get("answers") or {}
    chosen = answers.get("actions") or {}
    # Jev's choice tells us the best action first; the ranked probabilities give
    # the rest without a second call. Fall back to tree order if it is silent.
    probabilities = chosen.get("probabilities") or {}
    ordered = sorted(
        candidates,
        key=lambda c: float(probabilities.get(c.to_id()) or 0.0),
        reverse=True,
    )
    top = [c for c in ordered if probabilities.get(c.to_id()) is not None][:5]
    if not top:
        top = ordered[:5]

    return {
        "window_title": window.get("title"),
        "window_class": window.get("class") or window.get("app_name"),
        "actions": [
            {"id": c.to_id(), "role": c.role, "label": c.label,
             "enabled": c.enabled}
            for c in top
        ],
        "top_action": chosen.get("choice"),
        "confidence": round(float(chosen.get("confidence") or 0.0), 3),
        "candidate_count": len(candidates),
        "cost_usd": float((payload.get("usage") or {}).get("cost") or 0.0),
        "elapsed_s": round(time.monotonic() - started, 2),
    }


def _notify(summary: str, body: str, urgency: str = "normal") -> None:
    """Best-effort desktop notification. Never raises into the action path."""
    try:
        subprocess.run(
            ["notify-send", "-a", "qwen-voice", "-u", urgency, summary, body],
            capture_output=True, timeout=5, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass


# --- panic stop -----------------------------------------------------------
#
# The flag is the same file `panic` (and therefore every input path) uses. These
# kept module-local so a test or caller can point `vision.PANIC_FILE` at a
# scratch path without touching the process-wide flag.

PANIC_FILE = panic.PANIC_FILE


def panicked() -> bool:
    """True while the user has hit the panic-stop hotkey."""
    try:
        return PANIC_FILE.exists()
    except OSError:
        return False


def clear_panic() -> bool:
    """Remove the panic flag. Returns True if it had been set."""
    try:
        PANIC_FILE.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _panic_error() -> VisionError:
    return VisionError(
        "stopped by the panic key (SUPER + SHIFT + ESCAPE); no input was sent. "
        "Press the key again to clear it and carry on."
    )


def _window_fingerprint(cfg: dict, pid: int, window_id: int) -> dict | None:
    """A comparable view of a window: title, and each element's label + state.

    Selection and enabled state are included on purpose: clicking a list row or
    folder usually only changes its selected flag, and a fingerprint of labels
    alone would call that "nothing changed" and retry a click that worked.

    Returns None when the window cannot be read at all (it closed, or cua-driver
    failed), so "gone" stays distinguishable from "empty". Never raises: a
    verification step must degrade to 'unknown', never to a false success.
    """
    try:
        tree = _run_driver(cfg, "get_window_state", {
            "pid": pid, "window_id": window_id, "include_screenshot": False,
        })
    except VisionError:
        return None
    elements = sorted(
        (
            str(e.get("label") or ""),
            bool(e.get("selected", False)),
            bool(e.get("enabled", True)),
        )
        for e in (tree.get("elements") or [])
    )
    return {
        "title": str(tree.get("window_title") or ""),
        "elements": elements,
        "count": len(elements),
    }


def _verify_outcome(before: dict | None, after: dict | None) -> tuple[str, str]:
    """Compare two fingerprints -> ('satisfied'|'unsatisfied'|'unknown', why).

    State comparison is the only honest signal for a click. An "element exists"
    predicate is NOT usable here: the element we clicked necessarily existed
    before the click, so it would report success unconditionally. (Measured: it
    did exactly that for "the Music folder".)

    A window that has gone away is 'unknown', not failure: closing a dialog is a
    common, intended result of a click, and cua-driver cannot prove whether it
    happened or the app crashed.
    """
    if after is None:
        return "unknown", "the window is gone (it may have closed as intended)"
    if before is None:
        return "unknown", "could not read the window state before the click"
    if before.get("title") != after.get("title"):
        return "satisfied", f"window changed to {after.get('title')!r}"
    if before.get("elements") != after.get("elements"):
        return "satisfied", "the window's contents or selection changed"
    return "unsatisfied", "nothing about the window changed"


def click_element(goal: str, window_hint: str | None = None,
                  cfg: dict | None = None, double: bool = False,
                  verify: bool | None = None) -> dict:
    """Find the element for `goal`, click it, and verify the outcome.

    This TAKES OVER the real mouse and keyboard focus for a moment. A desktop
    notification announces that before the first input is sent, so the user
    knows not to touch the mouse or keys while the agent drives.

    Delivery uses the project's existing input path (focus the window, move the
    pointer to the element's frame, then ydotool), not cua-driver's own click.
    That is deliberate and measured: on this Hyprland session cua-driver's
    accessibility click fails with an X11 `TranslateCoordinates` error on native
    Wayland windows, and both the background and foreground routes report
    `background_unavailable` / `foreground_unavailable` ("production Hyprland
    input plugin is unavailable"). Its AT-SPI *reads* work perfectly, and its
    element frames are screen-absolute, so the reliable split is:

        cua-driver  -> which element, and where
        ydotool     -> deliver the click

    After the click the window is re-read and compared with the state before it,
    and the result is reported in `verified` / `verification` /
    `verification_reason`. `unsatisfied` means no change was observable; it does
    NOT necessarily mean the click failed (a list selection may not be exposed
    in the tree), but the caller must not claim success.

    Retries are OFF by default. A retry is a second click; see the `retries`
    comment in DEFAULT_CONFIG for why that is unsafe in general. The panic key
    (SUPER + SHIFT + ESCAPE) suppresses any input while it is set.
    """
    cfg = cfg or load_config()
    if panicked():
        raise _panic_error()
    found = find_element(goal, window_hint, cfg)

    from .desktop import DesktopController
    controller = DesktopController()
    frame = found.get("frame") or {}
    if not frame or not frame.get("w") or not frame.get("h"):
        raise VisionError("the chosen element has no usable frame to click")
    x = int(frame["x"]) + int(frame["w"]) // 2
    y = int(frame["y"]) + int(frame["h"]) // 2

    # Announce the takeover before moving anything.
    if cfg.get("announceTakeover", True):
        target = found.get("window_title") or found.get("window_class") or "window"
        _notify(
            "Qwen is taking control",
            f"Clicking \"{found.get('label') or goal}\" in {target}. "
            "Don't use the mouse or keyboard for a moment.",
        )

    should_verify = cfg.get("verify", True) if verify is None else verify
    attempts = 1 + (int(cfg.get("retries", 0)) if should_verify else 0)
    settle = max(0.0, float(cfg.get("verifyDelayMs", 600)) / 1000.0)

    before = _window_fingerprint(cfg, found["pid"], found["window_id"]) \
        if should_verify else {}

    outcome, reason = "unknown", "verification disabled"
    for attempt in range(1, attempts + 1):
        if panicked():
            raise _panic_error()
        _throttle(int(cfg.get("minIntervalMs", 1200)))
        # Focus the target first: ydotool delivers to the focused surface.
        try:
            class_hint = found.get("window_class") or found.get("window_title") or ""
            if class_hint:
                controller.focus_window(class_hint)
        except Exception:  # noqa: BLE001 - focus is best-effort; click still tries
            pass
        time.sleep(0.4)
        controller.pointer_move(x, y)
        time.sleep(0.25)
        if double:
            effect = _double_click()
        else:
            effect = controller.mouse_click("left")

        if not should_verify:
            outcome, reason = "unknown", "verification disabled"
            break

        time.sleep(settle)
        after = _window_fingerprint(cfg, found["pid"], found["window_id"])
        outcome, reason = _verify_outcome(before, after)
        if outcome == "unsatisfied" and cfg.get("jevOutcome"):
            # The diff is blind to a subtle change; let Jev disambiguate. It only
            # ever upgrades unsatisfied -> satisfied (never downgrades a real
            # change), so this cannot lose a verified success.
            outcome, reason = verify_outcome_jev(cfg, goal, before, after)
        if outcome != "unsatisfied" or attempt >= attempts:
            # satisfied, unknown, or out of retries: stop and report honestly.
            break
        # Otherwise retry once: re-read so a moved element is re-targeted.
        try:
            found = find_element(goal, window_hint, cfg)
            frame = found.get("frame") or {}
            x = int(frame["x"]) + int(frame["w"]) // 2
            y = int(frame["y"]) + int(frame["h"]) // 2
        except VisionError:
            break

    result = {
        **found,
        "goal": goal,
        "clicked_at": [x, y],
        "effect": effect,
        "double": double,
        "delivery": "ydotool",
        "takeover": "announced" if cfg.get("announceTakeover", True) else "silent",
        "verified": outcome == "satisfied",
        "verification": outcome,
        "verification_reason": reason,
        "attempts": attempt,
    }
    if cfg.get("audit", True):
        from . import audit
        audit.log_click(result)
    return result


def _double_click() -> str:
    """Two left clicks via ydotool (open an item)."""
    import subprocess
    try:
        proc = subprocess.run(
            ["ydotool", "click", "--repeat", "2", "0xC0"],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VisionError(f"double-click failed: {exc}")
    if proc.returncode != 0:
        raise VisionError("double-click failed: ydotool returned an error")
    return "double-clicked left button"


def _press_key(key: str) -> str:
    """Press one named key via wtype (same virtual-keyboard path as typing).

    The caller is responsible for only passing names from a fixed safe list;
    this function does not accept free text.
    """
    import subprocess
    try:
        proc = subprocess.run(
            ["wtype", "-k", key],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VisionError(f"key press failed: {exc}")
    if proc.returncode != 0:
        raise VisionError(f"key press failed: {proc.stderr.strip() or 'wtype error'}")
    return f"pressed {key}"
