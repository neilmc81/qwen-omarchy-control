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

from . import triage

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
}

CONFIG_HOME = triage.CONFIG_HOME
CONFIG_FILE = Path(os.environ.get(
    "QWEN_VISION_CONFIG", CONFIG_HOME / "qwen-omarchy-control" / "vision.json"
))


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


def resolve_window(cfg: dict, hint: str | None) -> dict:
    """Pick a window by pid, or by matching a title/name substring."""
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
    jev_cfg = dict(triage.load_config())
    # Reuse the triage transport but with this module's model/endpoint/key.
    for key in ("model", "endpoint", "apiKeyEnv"):
        jev_cfg[key] = cfg.get(key) or jev_cfg.get(key)
    try:
        return triage.ask(json.dumps(state, ensure_ascii=False), questions, jev_cfg)
    except triage.TriageError as exc:
        raise VisionError(f"selection model unavailable: {exc}")


def select(cfg: dict, goal: str, window: dict, candidates: list[Candidate]) -> dict:
    """Ask Jev for one candidate id; return the chosen Candidate + metadata."""
    if not candidates:
        raise VisionError("no actionable elements found in this window")
    title = str(window.get("title") or window.get("app_name") or "")
    payload = _jev_ask(cfg, goal, title, candidates)
    answers = payload.get("answers") or {}
    choice = answers.get("candidate") or {}
    chosen_id = str(choice.get("choice") or "none")
    confidence = float(choice.get("confidence") or 0.0)
    by_id = {c.to_id(): c for c in candidates}
    if chosen_id == "none" or chosen_id not in by_id:
        raise VisionError(
            f"no supplied element matches the goal (model said {chosen_id!r})"
        )
    candidate = by_id[chosen_id]
    if confidence < float(cfg.get("minConfidence", 0.60)):
        raise VisionError(
            f"selection not confident enough ({confidence:.2f} < "
            f"{cfg.get('minConfidence', 0.60)}); use the OCR tools instead"
        )
    return {
        "candidate": candidate,
        "confidence": confidence,
        "probabilities": choice.get("probabilities") or {},
        "cost_usd": float((payload.get("usage") or {}).get("cost") or 0.0),
    }


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
    """Locate the element that satisfies `goal`. Raises VisionError otherwise."""
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


def _notify(summary: str, body: str, urgency: str = "normal") -> None:
    """Best-effort desktop notification. Never raises into the action path."""
    try:
        subprocess.run(
            ["notify-send", "-a", "qwen-voice", "-u", urgency, summary, body],
            capture_output=True, timeout=5, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def click_element(goal: str, window_hint: str | None = None,
                  cfg: dict | None = None, double: bool = False) -> dict:
    """Find the element for `goal` and click it.

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

    Verified: a Jev-selected "Documents" cell, clicked this way, changed the
    Nautilus window title from "Home" to "Documents".
    """
    cfg = cfg or load_config()
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
    return {**found, "clicked_at": [x, y], "effect": effect,
            "double": double, "delivery": "ydotool",
            "takeover": "announced" if cfg.get("announceTakeover", True) else "silent"}


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
