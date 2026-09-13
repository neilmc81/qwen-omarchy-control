"""DesktopController: the explicit, structured desktop operation surface.

Every method is a small subprocess invocation against the real interfaces this
machine exposes (hyprctl JSON, omarchy CLI, WirePlumber/PulseAudio). No method
accepts free-form shell input; arguments are validated and passed as argv lists.
Designed to be called both from the CLI and from the stdio MCP server.

Hyprland 0.56+ dispatcher syntax is Lua-shaped and called via
`hyprctl dispatch '<lua>'`. Verified against this machine's installed stub.

All command strings come from fixed code (hyprctl / wpctl / pactl / omarchy /
gtk-launch / xdg-open / wtype); user-supplied values only ever fill structured
arguments (workspace numbers, volume percents, URLs, window/class patterns).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import platform
import re
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import discovery
from .policy import classify, reject_sensitive_text

# ---------------------------------------------------------------------------
# Subprocess helpers (no shell=True anywhere in this file)
# ---------------------------------------------------------------------------

TIMEOUT = 8.0


def run(argv: list[str], timeout: float = TIMEOUT, env: dict | None = None) -> tuple[int, str]:
    """Run argv, return (returncode, stdout+stderr text). Never a shell."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, env=env,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s: {' '.join(argv)}"
    except FileNotFoundError:
        return 127, f"command not found: {argv[0]}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def hypr_env() -> dict | None:
    """Environment with HYPRLAND_INSTANCE_SIGNATURE resolved, or None.

    hyprctl refuses to run without the signature. A user background service does
    not inherit it from the graphical session, so we discover the running
    instance under $XDG_RUNTIME_DIR/hypr/ ourselves.
    """
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return None  # inherited from the interactive session
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    base = Path(runtime) / "hypr"
    if not base.is_dir():
        return None
    try:
        signatures = sorted(p.name for p in base.iterdir()
                            if (base / p.name / ".socket.sock").exists())
    except OSError:
        return None
    if not signatures:
        return None
    env = os.environ.copy()
    env["HYPRLAND_INSTANCE_SIGNATURE"] = signatures[0]
    return env


def hyprctl_json(*args: str) -> dict | list | None:
    """hyprctl <args> -j parsed, or None on failure."""
    env = hypr_env()
    rc, out = run(["hyprctl", "-j", *args], env=env)
    if rc != 0 or not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _dispatch(lua: str) -> tuple[int, str]:
    """Run a Hyprland Lua dispatcher."""
    return run(["hyprctl", "dispatch", lua], env=hypr_env())


def _lua_call(call: str, **args: str) -> str:
    """Build a Lua dispatcher call: hl.dsp.<call>({ k = "v", ... })."""
    inner = ", ".join(f'{k} = "{v}"' for k, v in args.items())
    return f"hl.dsp.{call}({{ {inner} }})"


FSENSITIVE_RE = re.compile(
    r"(password|passwd|ssn|credit|card|bank|token|secret|pin|otp)", re.I
)


# ---------------------------------------------------------------------------
# Main controller
# ---------------------------------------------------------------------------


class DesktopError(Exception):
    """Action failed with a message safe to echo back to the voice layer."""


def _fail(msg: str) -> "DesktopError":
    return DesktopError(msg)


@dataclass(frozen=True)
class ShortWindow:
    address: str
    class_: str
    title: str
    workspace: str
    initial_title: str
    pid: int

    @property
    def haystack(self) -> str:
        return " ".join(x for x in (self.class_, self.title, self.initial_title) if x)


class DesktopController:
    def _clients(self) -> list[dict]:
        data = hyprctl_json("clients")
        return data if isinstance(data, list) else []

    def _active(self) -> dict:
        data = hyprctl_json("activewindow")
        return data if isinstance(data, dict) else {}

    def _workspaces(self) -> list[dict]:
        data = hyprctl_json("workspaces")
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------ queries
    def get_active_window(self) -> dict:
        w = self._active()
        if not w or not w.get("address"):
            raise _fail("no active window")
        return {
            "address": w.get("address"),
            "class": w.get("class"),
            "title": w.get("title"),
            "workspace": (w.get("workspace") or {}).get("name"),
            "pid": w.get("pid"),
        }

    def list_windows(self, use_filter: bool = True) -> list[dict]:
        rows = []
        for c in self._clients():
            if c.get("hidden"):
                continue
            rows.append({
                "address": c.get("address"),
                "class": c.get("class"),
                "title": (c.get("title") or "")[:100],
                "workspace": (c.get("workspace") or {}).get("name"),
                "pid": c.get("pid"),
                "focusHistoryId": c.get("focusHistoryId"),
            })
        return rows

    def list_workspaces(self) -> list[dict]:
        rows = []
        for w in self._workspaces():
            if w.get("id", -1) >= 0:
                rows.append({
                    "id": w.get("id"),
                    "name": w.get("name"),
                    "monitor": w.get("monitor"),
                    "windows": w.get("windows"),
                    "hasfullscreen": w.get("hasfullscreen"),
                })
        rows.sort(key=lambda r: r["id"])
        return rows

    def get_monitors(self) -> list[dict]:
        data = hyprctl_json("monitors")
        return data if isinstance(data, list) else []

    # --------------------------------------------------------- state changes
    def switch_workspace(self, number: int) -> str:
        num = self._check_workspace(number)
        rc, out = _dispatch(_lua_call("focus", workspace=str(num)))
        if rc != 0:
            raise _fail(f"could not switch workspace: {out}")
        return f"switched to workspace {num}"

    def move_active_window_to_workspace(self, number: int, follow: bool = False) -> str:
        num = self._check_workspace(number)
        lua = _lua_call("window.move", workspace=str(num),
                        follow="true" if follow else "false")
        rc, out = _dispatch(lua)
        if rc != 0:
            raise _fail(f"could not move window: {out}")
        mark = " and followed" if follow else " (kept focus)"
        return f"moved active window to workspace {num}{mark}"

    def focus_window(self, app_or_title: str) -> dict:
        needle = (app_or_title or "").strip()
        if not needle:
            raise _fail("focus_window needs a name")
        windows = self._clients()
        if not windows:
            raise _fail("no windows are open")
        # Exact match first, then substring on class, then title.
        low = needle.lower()

        def score(c) -> int:
            hay = " ".join(
                str(c.get(k) or "") for k in
                ("class", "initialClass", "initialTitle", "title")
            ).lower()
            if hay == low:
                return 3
            if low in str(c.get("class") or "").lower() or low in str(c.get("initialClass") or "").lower():
                return 2
            if low in hay:
                return 1
            return 0

        best = max(windows, key=score, default=None)
        if best is None or score(best) == 0:
            raise _fail(f"no window matches {needle!r}")
        address = best.get("address")
        if not address:
            raise _fail("matched window has no address")
        rc, out = _dispatch(_lua_call("focus", window=address))
        if rc != 0:
            raise _fail(f"could not focus window: {out}")
        return {
            "address": address,
            "class": best.get("class"),
            "title": best.get("title"),
        }

    def launch_app(self, name: str) -> str:
        argv = discovery.launch_argv_for(name)
        if not argv:
            raise _fail(
                f"no installed application matches {name!r}. Name an installed "
                "desktop entry, or say 'terminal', 'browser', or 'files'."
            )
        rc, out = run(argv, timeout=15.0)
        if rc != 0:
            raise _fail(f"failed to launch {name!r}: " + (out or "unknown error"))
        return f"launched {name}"

    def open_url(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme.lower() not in ("http", "https"):
            raise _fail("open_url accepts only http(s) URLs")
        rc, out = run(["xdg-open", url.strip()], timeout=15.0)
        if rc != 0:
            raise _fail(f"xdg-open failed: {out}")
        return f"opened {url}"

    def close_active_window(self) -> str:
        before = self._active_address()
        rc, out = _dispatch("hl.dsp.window.close()")
        if rc != 0:
            raise _fail(f"could not close window: {out}")
        # Verify the focused window actually went away.
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if self._active_address() != before:
                return "closed active window"
            time.sleep(0.05)
        return "close sent for active window"

    def _active_address(self) -> str | None:
        w = self._active()
        return w.get("address") if w else None

    # ----------------------------------------------------------------- audio
    def _volume_rc(self) -> tuple[int | None, float | None]:
        rc, out = run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
        if rc != 0:
            return None, None
        try:
            percents = re.findall(r"(\d+)%", out)
            return None, float(percents[0]) if percents else None
        except (IndexError, ValueError):
            return None, None

    def get_audio_status(self) -> dict:
        status: dict = {}
        rc, out = run(["pactl", "get-default-sink"])
        status["default_sink"] = out if rc == 0 else None
        _, volume = self._volume_rc()
        status["volume"] = volume
        rc, out = run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
        status["muted"] = bool(re.search(r"\b(yes|1)\b", out or "", re.I))
        rc, out = run(["pactl", "get-default-source"])
        status["default_source"] = out if rc == 0 else None
        rc, out = run(["pactl", "get-source-mute", "@DEFAULT_SOURCE@"])
        status["mic_muted"] = bool(re.search(r"\b(yes|1)\b", out or "", re.I))
        return status

    def set_volume(self, percent: int) -> str:
        pct = int(percent)
        if not (0 <= pct <= 100):
            raise _fail("volume must be between 0 and 100")
        rc, out = run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{pct}%"])
        if rc != 0:
            raise _fail(f"could not set volume: {out}")
        return f"volume set to {pct}%"

    def volume_up(self) -> str:
        rc, out = run(["omarchy", "audio", "output", "volume", "raise"], timeout=15.0)
        if rc != 0:
            raise _fail(f"volume up failed: {out}")
        _, volume = self._volume_rc()
        return f"volume raised to {volume}%" if volume else "volume raised"

    def volume_down(self) -> str:
        rc, out = run(["omarchy", "audio", "output", "volume", "lower"], timeout=15.0)
        if rc != 0:
            raise _fail(f"volume down failed: {out}")
        _, volume = self._volume_rc()
        return f"volume lowered to {volume}%" if volume else "volume lowered"

    def mute_audio(self) -> str:
        rc, out = run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"])
        if rc != 0:
            raise _fail(f"could not mute: {out}")
        return "muted output"

    def unmute_audio(self) -> str:
        rc, out = run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])
        if rc != 0:
            raise _fail(f"could not unmute: {out}")
        return "unmuted output"

    def get_system_status(self) -> dict:
        def rd(path: str) -> str:
            try:
                return Path(path).read_text().strip()
            except OSError:
                return ""

        status = {
            "hostname": platform.node(),
            "os": rd("/etc/os-release").splitlines()[0].replace("PRETTY_NAME=", "").strip('"')
                   or "unknown",
            "kernel": platform.release(),
            "time": _dt.datetime.now().strftime("%A %-d %B %Y, %H:%M %Z"),
        }
        try:
            status["uptime"] = subprocess.run(
                ["uptime", "-p"], capture_output=True, text=True, timeout=5,
                stdin=subprocess.DEVNULL,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            status["memory"] = subprocess.run(
                ["free", "-h"], capture_output=True, text=True, timeout=5,
                stdin=subprocess.DEVNULL,
            ).stdout.splitlines()[1].split()
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
        return status

    # ----------------------------------------------------------- validation
    def _check_workspace(self, number: int) -> int:
        try:
            num = int(number)
        except (TypeError, ValueError):
            raise _fail(f"workspace must be a number, got {number!r}")
        if num < 1 or num > 100:
            raise _fail("workspace number out of range (1-100)")
        return num

    # ---------------------------------------------------------- text typing
    def type_text(self, text: str) -> str:
        text = text.strip()
        if not text:
            raise _fail("nothing to type")
        if len(text) > 500:
            raise _fail("text too long")
        if reject_sensitive_text(text):
            raise _fail(
                "refused: text looks sensitive (password/secret/card). "
                "The voice assistant never types into sensitive fields."
            )
        rc, out = run(["wtype", text], timeout=8.0)
        if rc != 0:
            # Fallback: drive through hyprland's send_shortcut? No - keep it simple.
            raise _fail(f"wtype failed: {out}")
        return "typed text"

    # ----------------------------------------------------------- dispatcher
    def execute(self, operation: str, **kwargs):
        """Dispatch by name to a method (used by CLI and MCP)."""
        classify(operation)  # raises on unknown/denied
        method = getattr(self, operation, None)
        if method is None:
            raise _fail(f"not implemented: {operation}")
        result = method(**kwargs)
        if isinstance(result, dict):
            return result
        return {"result": str(result) if result is not None else "done"}


__all__ = ["DesktopController", "DesktopError", "run"]