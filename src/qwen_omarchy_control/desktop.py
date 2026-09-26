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
from .panic import guard
from .policy import classify, reject_sensitive_text

# ---------------------------------------------------------------------------
# Subprocess helpers (no shell=True anywhere in this file)
# ---------------------------------------------------------------------------

TIMEOUT = 8.0


def session_env(extra: dict | None = None) -> dict:
    """Process environment with the graphical-session vars resolved.

    Why this exists
    ---------------
    The MCP server is spawned by the gateway, a systemd *user* service that
    starts at boot — before Hyprland has created its Wayland socket. The env it
    inherits is therefore missing `WAYLAND_DISPLAY` (and often `DISPLAY`). Every
    GUI subprocess needs those: `grim` (screen capture), `wtype` (typing),
    `gtk-launch` (opening Chrome) and `foot` (agent windows) all fail without
    them. Measured: with the boot env, `read_screen` fails "grim capture
    failed"; with `WAYLAND_DISPLAY` set, it returns text.

    Resolving lazily (and per call, not once at startup) matters: at spawn the
    socket may not exist yet, so a value cached then would stay empty forever.
    This is re-checked on each use, so the long-lived server picks the socket up
    as soon as it appears. `bin/desktop-mcp` does the same for the initial
    process env; this covers everything spawned afterwards too.
    """
    env = os.environ.copy()
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not env.get("WAYLAND_DISPLAY"):
        try:
            for name in sorted(os.listdir(runtime)):
                if name.startswith("wayland-") and not name.endswith(".lock"):
                    env["WAYLAND_DISPLAY"] = name
                    break
        except OSError:
            pass
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        base = Path(runtime) / "hypr"
        try:
            for entry in sorted(base.iterdir()):
                if (entry / ".socket.sock").exists():
                    env["HYPRLAND_INSTANCE_SIGNATURE"] = entry.name
                    break
        except OSError:
            pass
    if not env.get("DISPLAY") and Path("/tmp/.X11-unix/X0").exists():
        env["DISPLAY"] = ":0"
    # Desktop notifications and pactl use the session bus; the gateway service
    # has it but the stdio child it spawns may not.
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        bus = Path(runtime) / "bus"
        if bus.exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    if extra:
        env.update(extra)
    return env


def run(argv: list[str], timeout: float = TIMEOUT, env: dict | None = None) -> tuple[int, str]:
    """Run argv, return (returncode, stdout+stderr text). Never a shell."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, env=env if env is not None else session_env(),
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s: {' '.join(argv)}"
    except FileNotFoundError:
        return 127, f"command not found: {argv[0]}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def run_bin(argv: list[str], timeout: float = TIMEOUT) -> tuple[int, bytes]:
    """Run argv and return binary stdout (for grim/screenshots)."""
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=session_env())
    except subprocess.TimeoutExpired:
        return 124, b"timed out"
    except FileNotFoundError:
        return 127, b"command not found"
    return proc.returncode, proc.stdout


def _ocr(png: bytes, psm: str = "3") -> tuple[int, str]:
    """tesseract over PNG bytes."""
    import tempfile
    env = os.environ.copy()
    # Tesseract's OpenMP worker pool livelocks under heavy system load: on this
    # 4-core box at load ~9 every psm/lang combo hit the 25s timeout (rc=124),
    # which surfaced as "tesseract OCR failed" from read_window/read_screen.
    # Single-threaded (OMP_THREAD_LIMIT=1) the identical screenshot OCRs in ~11s.
    env["OMP_THREAD_LIMIT"] = "1"
    with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp:
        tmp.write(png)
        tmp.flush()
        return run(["tesseract", tmp.name, "-", "--psm", psm, "-l", "eng"],
                   timeout=25.0, env=env)


def hypr_env() -> dict:
    """Environment with the Hyprland signature (and Wayland vars) resolved.

    hyprctl refuses to run without the signature, and the gateway-spawned server
    does not inherit it (nor `WAYLAND_DISPLAY`) from the graphical session, so
    it is discovered under $XDG_RUNTIME_DIR on each call. `session_env` does the
    discovery; this is a thin alias kept so existing callers read clearly.
    """
    return session_env()


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

# Mouse control. The compositor moves the pointer (hl.dsp.cursor.move); clicks
# and the wheel come from the ydotool daemon (uinput), which Hyprland does not
# expose through its dispatcher. Wheel distances are the application's numbers,
# not the compositor's: one notch is ~40 px in Chromium and most GTK apps.
SCROLL_PIXELS_PER_CLICK = 40
SCROLL_PAGE_OVERLAP = 0.85
SCROLL_MIN_CLICKS, SCROLL_MAX_CLICKS = 4, 30
SCROLL_SIGN = {"down": -1, "up": 1, "right": 1, "left": -1}
# ydotool button codes: 0xC0 is press+release, +1 right, +2 middle.
YDOTOOL_BUTTONS = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}
YDOTOOL_UNAVAILABLE = (
    "mouse control needs the ydotool daemon, which is not running. Start it "
    "with: systemctl --user enable --now ydotool.service"
)


def _scroll_clicks(height: int, pages: int) -> int:
    """Wheel notches for `pages` screenfuls of a window `height` px tall."""
    per_page = round(height * SCROLL_PAGE_OVERLAP / SCROLL_PIXELS_PER_CLICK)
    per_page = max(SCROLL_MIN_CLICKS, min(per_page, SCROLL_MAX_CLICKS))
    return per_page * pages


# Spoken names -> window match terms. "opencode"/"coding agent" matches the
# omarchy-launched agent terminal (class org.omarchy.agent, title "OC | ...").
WINDOW_ALIASES = {
    "opencode": ("qwen-opencode", "org.omarchy.agent", "oc |", "oc ", "opencode"),
    "coding agent": ("qwen-hermes", "qwen-opencode", "org.omarchy.agent", "oc |", "opencode"),
    "codex": ("qwen-codex", "codex"),
    "chatgpt": ("chatgpt", "openai"),
    "hermes": ("qwen-hermes", "hermes"),
    "terminal": ("org.omarchy.terminal", "ghostty", "foot", "alacritty", "kitty"),
    "browser": ("google-chrome", "chromium", "firefox"),
    "file manager": ("org.gnome.Nautilus", "nautilus"),
}

# Visible agent windows: open the agent's TUI pre-seeded with a spoken prompt,
# mirroring Omarchy's own per-agent default-agent flags (omarchy-agent script):
# hermes --yolo, codex --approve-for-me, opencode --auto. foot sets the Wayland
# app-id and -H keeps the window open after the agent finishes.
AGENT_LAUNCHERS = {
    "hermes": lambda p: ["foot", "-H", "--app-id=qwen-hermes", "-T", "Hermes Agent",
                         "env", "-u", "HERMES_SESSION_SOURCE",
                         *(["hermes", "chat", "--yolo", "--tui", f"--query={p}"]
                           if p else ["hermes", "--yolo"])],
    "codex": lambda p: ["foot", "-H", "--app-id=qwen-codex", "-T", "Codex",
                        *(["codex", "--approve-for-me", "--", p]
                          if p else ["codex", "--approve-for-me"])],
    "opencode": lambda p: ["foot", "-H", "--app-id=qwen-opencode", "-T", "OpenCode",
                           *(["opencode", "--auto", "--prompt", p]
                             if p else ["opencode", "--auto"])],
}


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

    # ------------------------------------------------------------------- mouse
    @staticmethod
    def _ydotool_socket() -> str:
        return (os.environ.get("YDOTOOL_SOCKET")
                or f"/run/user/{os.getuid()}/.ydotool_socket")

    def _ydotool_ready(self) -> bool:
        return Path(self._ydotool_socket()).exists()

    def pointer_move(self, x: int, y: int, relative: bool = False) -> str:
        """Move the pointer (absolute screen coords, or a delta when relative)."""
        guard("a pointer move")
        try:
            nx, ny = int(x), int(y)
        except (TypeError, ValueError):
            raise _fail("pointer coordinates must be integers")
        if relative:
            rc, out = run(["hyprctl", "cursorpos"], env=hypr_env())
            if rc != 0 or "," not in out:
                raise _fail("could not read the current pointer position")
            try:
                cx, cy = (int(p.strip()) for p in out.split(",", 1))
            except ValueError:
                raise _fail("could not parse the current pointer position")
            nx, ny = cx + nx, cy + ny
        rc, out = _dispatch(_lua_call("cursor.move", x=str(nx), y=str(ny)))
        if rc != 0:
            raise _fail(f"could not move the pointer: {out}")
        return f"pointer moved to {nx},{ny}"

    def mouse_click(self, button: str = "left", double: bool = False) -> str:
        guard("a click")
        button = (button or "left").strip().lower()
        code = YDOTOOL_BUTTONS.get(button)
        if code is None:
            raise _fail(f"button must be one of {', '.join(YDOTOOL_BUTTONS)}")
        if not self._ydotool_ready():
            raise _fail(YDOTOOL_UNAVAILABLE)
        argv = ["ydotool", "click"]
        if double:
            argv += ["--repeat", "2"]
        argv.append(code)
        rc, out = run(argv, timeout=10.0)
        if rc != 0:
            raise _fail(f"click failed: {out}")
        return f'{"double-" if double else ""}clicked {button} button'

    def mouse_scroll(self, direction: str, pages: int = 1,
                     window: str | None = None) -> str:
        """Turn the wheel over a window (or wherever the pointer already is).

        Pointing at the window first is not decoration: a wheel event goes to
        whatever is under the cursor, so without the move the scroll would land
        on whichever pane the mouse happens to be resting over.
        """
        guard("a scroll")
        direction = (direction or "").strip().lower()
        if direction not in SCROLL_SIGN:
            raise _fail(f"direction must be one of {', '.join(SCROLL_SIGN)}")
        try:
            pages = int(pages)
        except (TypeError, ValueError):
            raise _fail("pages must be a number")
        pages = max(1, min(pages, 10))
        if not self._ydotool_ready():
            raise _fail(YDOTOOL_UNAVAILABLE)
        win = self._resolve_window(window) if window else (self._active() or None)
        geo = self._window_geometry(win) if win else None
        if geo:
            wx, wy, ww, wh = geo
            _dispatch(_lua_call("cursor.move",
                                x=str(wx + ww // 2), y=str(wy + wh // 2)))
        else:
            ww = wh = 400
        span = ww if direction in ("left", "right") else wh
        clicks = _scroll_clicks(span, pages) * SCROLL_SIGN[direction]
        axis = "-x" if direction in ("left", "right") else "-y"
        other = "-y" if axis == "-x" else "-x"
        rc, out = run(["ydotool", "mousemove", "--wheel",
                       axis, str(clicks), other, "0"], timeout=10.0)
        if rc != 0:
            raise _fail(f"scroll failed: {out}")
        screens = "a screen" if pages == 1 else f"{pages} screens"
        return f"scrolled {direction} about {screens}"

    def _resolve_window(self, hint: str) -> dict | None:
        """Find a window by alias, class, or title substring.

        Aliases let the model address the user's real apps naturally:
        "opencode" / "the coding agent" matches the omarchy agent terminal that
        runs OpenCode (class org.omarchy.agent or a title starting with "OC").
        """
        needle = (hint or "").strip()
        if not needle:
            return self._active() or None
        aliases = WINDOW_ALIASES.get(needle.lower(), (needle.lower(),))
        best = None
        best_score = 0
        for c in self._clients():
            if c.get("hidden"):
                continue
            hay = " ".join(
                str(c.get(k) or "") for k in
                ("class", "initialClass", "initialTitle", "title")
            ).lower()
            for alias in aliases:
                if not alias:
                    continue
                if hay == alias:
                    score = 3
                elif alias in str(c.get("class") or "").lower() or alias in str(c.get("initialClass") or "").lower():
                    score = 2
                elif alias in hay:
                    score = 1
                else:
                    continue
                if score > best_score:
                    best, best_score = c, score
        return best

    def focus_window(self, app_or_title: str) -> dict:
        needle = (app_or_title or "").strip()
        if not needle:
            raise _fail("focus_window needs a name")
        best = self._resolve_window(needle)
        if best is None:
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
        # Launch detached with the std streams closed. Waiting on the launcher
        # does not work for GUI apps: `gtk-launch` (Chrome, and anything using
        # it) forks the real process, which then holds the inherited stdout pipe
        # open, so `subprocess.run(capture_output=True)` blocks until the app
        # exits and we wrongly report "timed out after 15s". Measured: a
        # launcher that backgrounds a 6s child blocks a capture_output run for
        # the full 6s. Starting detached sidesteps the pipe entirely; the window
        # appearing is the real success signal, not the launcher's exit code.
        is_terminal = argv[0] in ("foot", "ghostty", "alacritty", "kitty", "wezterm")
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL,
                             env=session_env(), start_new_session=True)
        except FileNotFoundError:
            raise _fail(f"command not found: {argv[0]}")
        if is_terminal:
            return f"launched {name} in a terminal window"
        return f"launched {name}"

    def launch_agent(self, agent: str, prompt: str = "") -> str:
        """Open a VISIBLE agent TUI window.

        With a prompt: the agent TUI opens pre-seeded with the task, so the
        user watches it work (the coding-task path). With an empty prompt: a
        fresh interactive session opens (e.g. "open hermes tui"). All agent
        launches mirror Omarchy's per-agent default flags (--yolo / --auto).
        Level 2 - with a prompt it submits work to an agent.
        """
        key = (agent or "").strip().lower()
        launcher = AGENT_LAUNCHERS.get(key)
        if launcher is None:
            raise _fail(f"unknown agent {agent!r}; use hermes, codex or opencode")
        prompt = (prompt or "").strip()
        if len(prompt) > 800:
            raise _fail("prompt too long")
        if prompt and reject_sensitive_text(prompt):
            raise _fail("refused: prompt looks sensitive; agents never handle "
                        "passwords/secrets from the voice channel")
        argv = launcher(prompt)
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL,
                             env=session_env(), start_new_session=True)
        except FileNotFoundError:
            raise _fail(f"command not found: {argv[0]}")
        if prompt:
            return f"opened {key} in a window with your request"
        return f"opened {key} in a fresh window"

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

    @staticmethod
    def _pretty_os_name() -> str:
        """PRETTY_NAME from /etc/os-release, or a NAME/ID fallback.

        /etc/os-release is a KEY=value file, not positional. Reading line 0 and
        stripping "PRETTY_NAME=" from it returned `NAME="Omarchy` on this
        machine (NAME comes first).
        """
        try:
            text = Path("/etc/os-release").read_text()
        except OSError:
            return platform.system() or "unknown"
        fields: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip().strip('"').strip("'")
        return (fields.get("PRETTY_NAME") or fields.get("NAME")
                or fields.get("ID") or platform.system() or "unknown")

    def get_system_status(self) -> dict:
        status = {
            "hostname": platform.node(),
            "os": self._pretty_os_name(),
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

    # ------------------------------------------------------------ OCR read-back
    OCR_LIMIT = 4000

    def _window_geometry(self, win: dict) -> tuple[int, int, int, int] | None:
        """(x, y, width, height) of a client, or None when not capturable."""
        at, size = win.get("at"), win.get("size")
        if not at or not size or len(at) < 2 or len(size) < 2:
            return None
        return int(at[0]), int(at[1]), int(size[0]), int(size[1])

    def read_window(self, window: str | None = None) -> str:
        """OCR a window (grim screenshot -> tesseract) and return its text.

        This is how the assistant reads back what an app/agent produced (e.g.
        the coding agent's answer) so it can summarize it out loud.
        """
        win = self._resolve_window(window) if window else (self._active() or None)
        if win is None:
            raise _fail("no window to read")
        geo = self._window_geometry(win)
        if geo is None:
            raise _fail(f"cannot capture window geometry for {win.get('class')}")
        x, y, w, h = geo
        if w <= 0 or h <= 0:
            raise _fail("window has zero size")
        rc, png = run_bin(["grim", "-s", "0.75", "-g", f"{x},{y} {w}x{h}", "-"], timeout=15.0)
        if rc != 0 or not png:
            raise _fail("grim capture failed")
        rc, out = _ocr(png)
        if rc != 0:
            raise _fail("tesseract OCR failed")
        text = out.strip()
        if not text:
            return f"({win.get('class') or 'window'} shows no readable text)"
        if len(text) > self.OCR_LIMIT:
            text = text[: self.OCR_LIMIT] + " ..."
        return text

    def read_screen(self) -> str:
        """OCR the whole focused monitor (for a broad answer)."""
        mon = hyprctl_json("activewindow") or {}
        data = hyprctl_json("monitors")
        if not data:
            raise _fail("no monitor to capture")
        target = None
        for m in data if isinstance(data, list) else []:
            if m.get("focused"):
                target = m
                break
        if target is None and isinstance(data, list) and data:
            target = data[0]
        if target is None:
            raise _fail("no monitor to capture")
        geo = (int(target["x"]), int(target["y"]),
               int(target["width"]), int(target["height"]))
        x, y, w, h = geo
        rc, png = run_bin(["grim", "-s", "0.75", "-g", f"{x},{y} {w}x{h}", "-"], timeout=15.0)
        if rc != 0 or not png:
            raise _fail("grim capture failed")
        rc, out = _ocr(png)
        if rc != 0:
            raise _fail("tesseract OCR failed")
        text = out.strip()
        return text[: self.OCR_LIMIT] + (" ..." if len(text) > self.OCR_LIMIT else "")

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
    def type_text(self, window: str | None, text: str, send: bool = False) -> str:
        """Type `text` into a target window (focus first).

        With `send=True`, presses Return afterwards so a prompt lands in the
        target app (e.g. to hand a request to the user's coding agent).
        Level 2: ordinary, non-sensitive windows only.
        """
        guard("typed text")
        text = (text or "").strip()
        if not text:
            raise _fail("nothing to type")
        if len(text) > 500:
            raise _fail("text too long")
        if reject_sensitive_text(text):
            raise _fail(
                "refused: text looks sensitive (password/secret/card). "
                "The voice assistant never types into sensitive fields."
            )
        win = self._resolve_window(window) if window else (self._active() or None)
        if win is None:
            raise _fail("no target window to type into")
        address = win.get("address")
        if address:
            _dispatch(_lua_call("focus", window=address))
            time.sleep(0.25)
        rc, out = run(["wtype", text], timeout=8.0)
        if rc != 0:
            raise _fail(f"could not type text: {out}")
        if send:
            rc, out = run(["wtype", "-k", "Return"], timeout=8.0)
            if rc != 0:
                raise _fail(f"typed text but could not press Enter: {out}")
            return f"typed and sent into {win.get('class') or 'window'}"
        return f"typed into {win.get('class') or 'window'} (not sent)"

    # ----------------------------------------------------------- dispatcher
    # Operations the MCP layer must confirm before running (see mcp.py). Kept on
    # the controller so the description logic lives next to the code that knows
    # the live window state.
    GATED = frozenset({
        "close_active_window",
        "move_active_window_to_workspace",
        "set_volume",
    })

    def describe(self, operation: str, **kwargs) -> str:
        """A short human description of a gated operation, for confirmation."""
        if operation == "close_active_window":
            w = self._active()
            who = "the active window"
            if w:
                who = w.get("class") or w.get("initialClass") or who
                title = (w.get("title") or "").strip()
                if title and title.lower() != who.lower():
                    who = f"{who} — {title[:40]}"
            return f"close {who}"
        if operation == "move_active_window_to_workspace":
            w = self._active()
            who = "the active window"
            if w:
                who = w.get("class") or w.get("initialClass") or who
            return f"move {who} to workspace {kwargs.get('number')}"
        if operation == "set_volume":
            return f"set the volume to {kwargs.get('percent')}%"
        return operation.replace("_", " ")

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