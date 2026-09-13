"""Desktop entry discovery.

Builds a launch lookup from the real installed system (XDG .desktop files) plus a
small alias table for names that are Omarchy routes rather than desktop entries
(terminal, browser, file manager, editor). Nothing here is hard-coded to a
machine-specific path or window.

Design notes
------------
* Apps are resolved by exact desktop id first, then exact Name, then a fuzzy
  contains-match on Name/GenericName/Keywords/StartupWMClass.
* Web apps and normal apps are treated the same: launch via the desktop entry
  so whatever the entry really does (a Chromium --app window, a native binary
  with a Wayland app id, ...) is preserved.
* Only launchers that exec the wanted program are considered; anything whose
  Exec mentions curl/wget/sh -c and treats itself as an installer is skipped.
"""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass
from pathlib import Path

Home = Path.home()

# Spoken agent names -> a VISIBLE terminal TUI window (plain open, no prompt).
# foot sets the Wayland app-id so the assistant can focus/type into it. Flags
# mirror Omarchy's per-agent default-agent launch (hermes --yolo, etc.).
AGENT_TUIS = {
    "hermes": ["foot", "-H", "--app-id=qwen-hermes", "-T", "Hermes Agent",
               "env", "-u", "HERMES_SESSION_SOURCE", "hermes", "--yolo"],
    "codex": ["foot", "-H", "--app-id=qwen-codex", "-T", "Codex",
              "codex", "--approve-for-me"],
    "opencode": ["foot", "-H", "--app-id=qwen-opencode", "-T", "OpenCode",
                 "opencode", "--auto"],
}

APP_DIRS = [
    Path(os.environ.get("XDG_DATA_HOME", Home / ".local/share")) / "applications",
    Home / ".local/share/applications",
    Path("/usr/local/share/applications"),
    Path("/usr/share/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
]

_OMARCHY_ROUTES = {
    "terminal": ["omarchy", "launch", "terminal"],
    "browser": ["omarchy", "launch", "browser"],
    "file manager": ["omarchy", "launch", "nautilus"],
    "files": ["omarchy", "launch", "nautilus"],
    "editor": ["omarchy", "launch", "editor"],
    "text editor": ["omarchy", "launch", "editor"],
}

# Common spoken names that should map to the *default* thing Omarchy considers
# canonical, rather than a random installed entry.
ALIAS_EXTRA = {
    "chrome": "google-chrome",
    "terminal": "@route:terminal",
    "browser": "@route:browser",
    "file manager": "@route:file manager",
    "files": "@route:files",
    "editor": "@route:editor",
    "code editor": "@route:editor",
    "discord": "@webapp:discord",
}

# Exec lines that indicate an installer/profile-wizard rather than a plain
# launcher; skip them so "open chrome" never starts an installer.
_INSTALLER_RE = re.compile(r"(curl|wget|install|setup|\.sh|python\s+-\w*\s*-m\s+pip)", re.I)


@dataclass
class App:
    id: str
    name: str
    generic_name: str
    keywords: str
    exec_: str
    startup_wm_class: str
    hidden: bool
    nodisplay: bool
    desktop_path: Path | None = None

    @property
    def tokens(self) -> str:
        return " ".join(
            x for x in (self.id, self.name, self.generic_name, self.keywords,
                        self.startup_wm_class) if x
        )

    def matches(self, needle: str) -> bool:
        return needle.lower() in self.tokens.lower()


def _read_desktop(path: Path) -> App | None:
    # strict=False: .desktop files frequently repeat keys (e.g. two
    # StartupWMClass lines in google-chrome.desktop); last one wins.
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError):
        return None
    if not parser.has_section("Desktop Entry"):
        return None
    entry = parser["Desktop Entry"]
    try:
        exec_ = entry.get("Exec", "").strip()
    except Exception:
        return None
    if parser.getboolean("Desktop Entry", "Hidden", fallback=False):
        return None
    if parser.getboolean("Desktop Entry", "NoDisplay", fallback=False):
        return None
    # GUI-only entries; skip terminal helpers with no desktop.
    if "NoDisplay" in entry and parser.getboolean("Desktop Entry", "NoDisplay"):
        return None
    return App(
        id=path.stem,
        name=entry.get("Name", path.stem).strip(),
        generic_name=entry.get("GenericName", "").strip(),
        keywords=entry.get("Keywords", "").strip(),
        exec_=exec_,
        startup_wm_class=entry.get("StartupWMClass", "").strip(),
        hidden=False,
        nodisplay=False,
        desktop_path=path,
    )


def _is_launchable(app: App) -> bool:
    if app.hidden or app.nodisplay:
        return False
    if not app.exec_:
        return False
    if _INSTALLER_RE.search(app.exec_):
        return False
    return True


def all_apps() -> list[App]:
    """Every launchable GUI entry across the standard XDG directories."""
    seen: dict[str, App] = {}
    for base in APP_DIRS:
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.desktop")):
            app = _read_desktop(path)
            if app is None:
                continue
            # Prefer the most specific dir; last write wins.
            seen[app.id] = app
    return [app for app in seen.values() if _is_launchable(app)]


def resolve_route(alias: str) -> list[str] | None:
    """Map an alias to an `omarchy` route argv, or None."""
    key = alias.strip().lower()
    if key in _OMARCHY_ROUTES:
        return list(_OMARCHY_ROUTES[key])
    direct = ALIAS_EXTRA.get(key)
    if direct == "@route:terminal":
        return list(_OMARCHY_ROUTES["terminal"])
    if direct == "@route:browser":
        return list(_OMARCHY_ROUTES["browser"])
    if direct == "@route:file manager":
        return list(_OMARCHY_ROUTES["file manager"])
    if direct == "@route:editor":
        return list(_OMARCHY_ROUTES["editor"])
    return None


def find_app(name: str) -> App | None:
    """Resolve a spoken app name/id to the best matching desktop entry."""
    needle = (name or "").strip()
    if not needle:
        return None
    apps = all_apps()
    if not apps:
        return None

    # 1. Exact desktop id (fastest, unambiguous).
    for app in apps:
        if app.id == needle:
            return app
    # 2. Exact Name.
    low = needle.lower()
    for app in apps:
        if app.name.lower() == low or app.id.lower() == low:
            return app
    # 3. Tokens contained (name bucket first).
    for app in apps:
        if app.name.lower() and needle.lower() in app.name.lower():
            return app
    for app in apps:
        if app.id.lower() and needle.lower() in app.id.lower():
            return app
    # 4. StartupWMClass handles "what is this app's window called".
    for app in apps:
        if app.startup_wm_class.lower() and needle.lower() in app.startup_wm_class.lower():
            return app
    # 5. Fuzzy: all significant words present somewhere.
    words = re.findall(r"[a-z0-9]+", low)
    if words:
        for app in apps:
            hit = all(w in app.tokens.lower() for w in words)
            if hit:
                return app
    return None


def launch_argv(app: App) -> list[str]:
    """The argv that starts `app` via its desktop entry."""
    gtk = "/usr/bin/gtk-launch"
    if app.desktop_path and app.desktop_path.exists():
        if os.access(gtk, os.X_OK):
            return [gtk, f"{app.id}.desktop"]
        return ["gio", "launch", str(app.desktop_path)]
    return ["gtk-launch", f"{app.id}.desktop"]


def launch_argv_for(name: str) -> list[str] | None:
    """Best-effort argv for a spoken name. None when unresolvable.

    Prefers a visible agent TUI (hermes/codex/opencode), then omarchy routes,
    then desktop discovery."""
    agent = AGENT_TUIS.get((name or "").strip().lower())
    if agent:
        return list(agent)
    argv = resolve_route(name)
    if argv:
        return argv
    app = find_app(name)
    return launch_argv(app) if app else None


def summarize(app: App) -> str:
    return f"{app.name} ({app.id})"