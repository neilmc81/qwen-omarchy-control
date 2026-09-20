"""Typed browser control via cua-driver's CDP binding (no OCR).

Why this exists
---------------
Chrome is the window the user works in most, and it is exactly the window the
accessibility-tree path cannot reach: Chrome exposes no AT-SPI tree, so
`find_element` / `describe_actions` report `degraded` and the only fallback is
OCR - tesseract reading a picture of the page. cua-driver exposes a second,
better channel: bind to the browser's own DevTools endpoint (CDP) and address
real DOM elements by role and name. This module wraps that channel.

Measured on this machine (not from docs)
----------------------------------------
* Binding requires the browser to have a DevTools endpoint. A running Chrome
  without `--remote-debugging-port` is refused:
  `browser_requires_setup ... relaunch the browser with --remote-debugging-port`.
  The exact-window setup path also refuses here (`browser_binding_ambiguous`)
  because it cannot attribute one of a mutli-window Chrome's accessibility
  top-levels to the requested window. So the port is the supported route.
* With the port present, `browser_prepare` succeeds and the rest follows:
  `get_browser_state` returned real element refs (`p1:0` role `link`, name
  "Learn more", actions `["click","pointer"]`) and an outline.
* `browser_click` with the default `trusted` route is **refused** on this
  Hyprland setup (`route_unavailable`, the same limitation as `click_element`).
  `input_route: "dom_event"` works and runs in the background - it does not move
  the real mouse at all. That is the route used here, and it is a genuine
  advantage: a browser click needs no takeover announcement.
* Verified end to end: a DOM click navigated example.com -> iana.org, and
  `browser_type` delivered text into DuckDuckGo's search box.

Safety
------
This reaches the user's *logged-in* profile, so it is treated as level 2 and
gated the same way as other changing actions. It can never move the real mouse.
While the panic flag is set, every changing call refuses. Read-only calls
(`browser_read`) still work while frozen, because looking is always allowed.
Downloads are not exposed here: `browser_download` writes files and belongs to
the coding backend, which has its own prompts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import panic, selection, triage

# --- config ---------------------------------------------------------------

DEFAULT_CONFIG = {
    # On by default, like the other vision tools: it self-degrades to a clear
    # message when no debug-enabled browser is present. Set false to remove it.
    "enabled": True,
    "driver": "cua-driver",
    "enableWayland": True,
    "driverTimeoutMs": 45000,
    # The label that owns browser targets/tabs/refs for a session.
    "session": "qwen",
    # The default click route. `dom_event` is a synthetic, full-background DOM
    # click: measured to work here, and it never moves the real mouse. The
    # default `trusted` route is refused on Hyprland (route_unavailable).
    "clickRoute": "dom_event",
    # Keep the number of elements returned to the model bounded.
    "maxElements": 60,
    # Pick page elements with Jev (System One) rather than a string match.
    # Falls back to the deterministic scorer when the model is unavailable.
    "selectWithJev": True,
    # Confidence floor for a Jev selection to be acted on.
    "minConfidence": 0.60,
    # Search engine used by the goal-level browser_search action.
    "searchEngine": "https://duckduckgo.com",
    "audit": True,
}

CONFIG_HOME = triage.CONFIG_HOME
CONFIG_FILE = Path(os.environ.get(
    "QWEN_BROWSER_CONFIG", CONFIG_HOME / "qwen-omarchy-control" / "browser.json"
))

# Debug ports worth probing for a bindable browser, in order.
_PROBE_PORTS = (9222, 9223, 9229)


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


class BrowserError(RuntimeError):
    """Browser control could not be completed; the caller uses its fallback."""


# --- cua-driver -----------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _driver_env(cfg: dict) -> dict:
    env = os.environ.copy()
    if cfg.get("enableWayland"):
        env["CUA_DRIVER_RS_ENABLE_WAYLAND"] = "1"
    return env


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


def _run_driver(cfg: dict, tool: str, args: dict) -> dict:
    driver = str(cfg.get("driver") or "cua-driver")
    timeout = max(1.0, float(cfg.get("driverTimeoutMs", 45000)) / 1000.0)
    try:
        proc = subprocess.run(
            [driver, "call", tool, json.dumps(args)],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, env=_driver_env(cfg),
        )
    except FileNotFoundError:
        raise BrowserError(f"{driver} is not installed")
    except subprocess.TimeoutExpired:
        raise BrowserError(f"{tool} timed out")
    except OSError as exc:
        raise BrowserError(f"{tool} could not run: {exc}")

    text = _ANSI_RE.sub("", (proc.stdout or "") + (proc.stderr or ""))
    payload = _extract_json(text)
    if payload is None:
        detail = text.strip().splitlines()[-1][:200] if text.strip() else "no output"
        raise BrowserError(f"{tool} returned no JSON ({detail})")
    if isinstance(payload, dict) and payload.get("refusal"):
        refusal = payload["refusal"]
        raise BrowserError(
            f"{tool} refused: {refusal.get('code') or refusal}"
        )
    return payload


# --- discovery ------------------------------------------------------------


def _browser_windows(cfg: dict) -> list[dict]:
    """Browser windows from cua-driver's list, newest pid first."""
    payload = _run_driver(cfg, "list_windows", {})
    windows = payload.get("windows") or []
    out = [w for w in windows if _is_browser(str(w.get("app_name") or ""))]
    # Prefer an on-screen window, but keep all browser windows.
    out.sort(key=lambda w: (not w.get("is_on_screen"),))
    return out


_BROWSER_NAMES = ("chrome", "chromium", "brave", "edge", "vivaldi", "opera")


def _is_browser(app_name: str) -> bool:
    low = (app_name or "").lower()
    return any(name in low for name in _BROWSER_NAMES)


def _debug_port_for(pid: int) -> int | None:
    """Which loopback DevTools port this pid owns, if any.

    The bindable endpoint is what makes typed control possible: without
    --remote-debugging-port there is nothing to attach to, and cua-driver
    rightly refuses rather than guessing.
    """
    for port in _PROBE_PORTS:
        try:
            proc = subprocess.run(
                ["ss", "-ltnpH", f"sport = :{port}"],
                capture_output=True, text=True, timeout=5,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and f"pid={pid}," in (proc.stdout or ""):
            return port
    return None


def _focused_geometry() -> dict | None:
    """The focused window's pid/title/size from Hyprland, for disambiguation.

    cua's window list carries no z-order or focus flag (measured: `z_index` is
    null for every window), so with several identically-titled Chrome windows
    there is nothing in the list itself to say which one the user is looking at.
    Hyprland knows, and its pid matches cua's entries.
    """
    from .desktop import DesktopController
    try:
        active = DesktopController().get_active_window()
    except Exception:  # noqa: BLE001 - focus lookup must never break targeting
        return None
    return active if active.get("address") else None


def _bind_candidates(cfg: dict) -> list[dict]:
    """Browser windows ordered by how likely they are the one to drive.

    Measured on this machine: cua-driver refuses to bind when a pid has several
    *identically titled* top-level windows (`browser_binding_ambiguous` /
    `authorization_host_failed`), but binds any of them fine when their titles
    differ. A browser window usually has a real page title, so in practice this
    is the blank-tab case. The fix is not to give up: try the best window, and if
    the driver refuses to attribute it, fall back to the next candidate.
    """
    cfg = cfg or load_config()
    windows = _browser_windows(cfg)
    if not windows:
        raise BrowserError("no browser window is open")

    focused = _focused_geometry()
    active_pid = int((focused or {}).get("pid") or 0)

    def rank(window: dict) -> tuple:
        pid = int(window.get("pid") or 0)
        title = str(window.get("title") or "")
        # Focused first, then on-screen, then a *titled* window over a blank one
        # (distinct titles are what makes binding possible), then area.
        return (
            0 if (active_pid and pid == active_pid and window.get("is_on_screen")) else 1,
            0 if window.get("is_on_screen") else 1,
            0 if (title and title.strip()) else 1,
            -(int(window.get("width") or 0) * int(window.get("height") or 0)),
            int(window.get("window_id") or 0),
        )

    bindable = []
    for window in windows:
        pid = int(window.get("pid") or 0)
        port = _debug_port_for(pid) if pid else None
        if port:
            window = dict(window, debug_port=port)
            bindable.append(window)
    if bindable:
        return sorted(bindable, key=rank)
    names = ", ".join(sorted({str(w.get("app_name")) for w in windows}))
    raise BrowserError(
        f"a browser is open ({names}) but without a DevTools endpoint, so its "
        "page cannot be read precisely. Relaunch it with "
        "--remote-debugging-port=9222 (see README: Typed browser control). "
        "Until then the OCR tools still work."
    )


def find_browser(cfg: dict | None = None) -> dict:
    """The best browser window to drive. Raises BrowserError if none is bindable.

    Returns the pid/window_id plus the debug port. When several browser windows
    share a pid, the focused one is preferred; note this only chooses the first
    *candidate* - the caller retries the rest if the driver refuses to attribute
    it (see `_bind_candidates`).
    """
    return _bind_candidates(cfg)[0]


def available(cfg: dict | None = None) -> tuple[bool, str]:
    cfg = cfg or load_config()
    if not cfg.get("enabled"):
        return False, "typed browser control is disabled (config enabled=false)"
    driver = str(cfg.get("driver") or "cua-driver")
    if not shutil.which(driver):
        return False, f"{driver} is not installed"
    return True, ""


# --- binding --------------------------------------------------------------


def _prepare(cfg: dict, window: dict) -> None:
    """Attach to the browser's DevTools endpoint under the existing-profile grant.

    The grant is a daemon-startup flag (`cua-driver serve --grant
    existing-profile`), not a per-call one; without it the driver refuses to
    touch a consumer profile. This is an explicit, user-authorised attachment to
    the logged-in browser, which is why browser actions are level 2.

    A named session can *end* (the driver reports "session '<label>' has ended"
    and refuses every later call). Ordinary actions never revive an ended name,
    so `start_session` is called first; it is idempotent and returns the live
    session when one already exists.
    """
    session = str(cfg.get("session") or "qwen")
    _run_driver(cfg, "start_session", {"session": session})
    _run_driver(cfg, "browser_prepare", {
        "pid": int(window["pid"]),
        "window_id": int(window["window_id"]),
        "strategy": {"kind": "existing_profile"},
        "session": session,
    })


def _state(cfg: dict, window: dict, **extra) -> dict:
    args = {"session": str(cfg.get("session") or "qwen"), "pid": int(window["pid"]),
            "window_id": int(window["window_id"])}
    args.update(extra)
    return _run_driver(cfg, "get_browser_state", args)


def _pair(cfg: dict, window: dict) -> tuple[str, str]:
    """A matched (target_id, tab_id) pair.

    The ids are session-scoped and rotate between calls, so they must be minted
    and used in the same sequence; using a stale pair is refused with
    `authorization_host_failed`.
    """
    state = _state(cfg, window)
    target = state.get("target_id")
    tabs = state.get("tabs") or []
    if not target or not tabs:
        raise BrowserError("the browser exposed no target/tab to bind to")
    # Prefer the active tab, exactly as _bind_and_read does; tabs[0] is not
    # necessarily the one the user is looking at.
    tab_info = next((t for t in tabs if t.get("active")), tabs[0])
    return str(target), str(tab_info.get("tab_id"))


_AMBIGUOUS = ("browser_binding_ambiguous", "authorization_host_failed",
              "browser_binding_stale", "browser_consent_required",
              "heuristic")


def _is_ambiguity(exc: Exception) -> bool:
    """True when the driver could not attribute a window, not a real failure.

    These refusals mean "this window could not be proven", which is retryable
    against another window; a missing endpoint or a bad key is not.
    """
    text = str(exc)
    return any(code in text for code in _AMBIGUOUS)


def _multi_window(cfg: dict) -> bool:
    """True when a browser pid owns more than one top-level window.

    Measured on this machine: cua-driver only binds *exactly* (bounds-correlated)
    when the browser exposes a single top-level window. With two, its binding is
    `heuristic` (title-only) and it refuses every element read and mutation with
    `authorization_host_failed: this binding is heuristic (title-only) —
    mutations require an exact bounds- or cardinality-correlated binding`. So the
    typed path is a single-window capability, and the honest thing is to say so
    rather than quietly returning an empty page.
    """
    try:
        windows = _browser_windows(cfg)
    except BrowserError:
        return False
    counts: dict[int, int] = {}
    for window in windows:
        pid = int(window.get("pid") or 0)
        counts[pid] = counts.get(pid, 0) + 1
    return any(count > 1 for count in counts.values())


def _single_window_error(cfg: dict) -> BrowserError:
    # This text is read by the voice model, so it must NOT invite a destructive
    # workaround. Observed live: a previous wording ("close the other browser
    # windows") led the model to offer to close the user's MarketOS window. The
    # fallback named here is read-only.
    return BrowserError(
        "the browser has more than one window open, and the precise (DOM) tools "
        "need exactly one to bind it exactly - with several, the driver falls "
        "back to a title-only binding and refuses to read or act. Do NOT close "
        "the user's windows. Report this in one sentence and use read_screen "
        "(OCR) on the window the user means instead."
    )


def _bind_and_read(cfg: dict, window: dict,
                   query: str | None = None) -> tuple[str, str, dict]:
    """Mint a (target_id, tab_id) pair and read the page in one sequence.

    The pair MUST be minted immediately before the read and then reused for any
    action on those refs: page refs are invalidated by a newer snapshot of the
    same tab, so calling `get_browser_state` again between reading and clicking
    silently invalidates the ref and the click becomes a no-op (measured: a
    "Learn more" click reported `dom` route success but never navigated).
    """
    # Reading via target_id/tab_id (not pid/window) mints the pair in this call.
    state = _state(cfg, window)
    target = state.get("target_id")
    tabs = state.get("tabs") or []
    if not target or not tabs:
        raise BrowserError("the browser exposed no target/tab to bind to")
    # Prefer the ACTIVE tab. Taking tabs[0] blindly was a real bug: a window with
    # several tabs can have the active one anywhere in the list, so browser_read
    # would report the wrong page (measured: it read an unrelated financial tab).
    tab_info = next((t for t in tabs if t.get("active")), tabs[0])
    tab = str(tab_info.get("tab_id"))
    extra = {"target_id": str(target), "tab_id": tab,
             "snapshot_format": "semantic_v2"}
    if query:
        extra["query"] = query
    sem = _run_driver(cfg, "get_browser_state", {
        "session": str(cfg.get("session") or "qwen"), **extra,
    })
    return str(target), tab, sem


def _with_browser(cfg: dict, fn):
    """Run `fn(window)` against the browser, refusing honestly when unusable.

    The typed path needs an exact binding, which the driver only produces when
    the browser owns a single top-level window. With several, every window binds
    heuristically and every element read/mutation is refused, so walk the
    candidates is pointless - say so clearly instead of returning an empty page.
    A pid with several *identically titled* windows is still retried, because a
    transient attribution failure there is worth one more candidate.
    """
    if _multi_window(cfg):
        raise _single_window_error(cfg)
    candidates = _bind_candidates(cfg)
    last: Exception | None = None
    for window in candidates:
        try:
            _prepare(cfg, window)
            return fn(window)
        except BrowserError as exc:
            if not _is_ambiguity(exc):
                raise
            last = exc
            continue
    if last is not None:
        raise last
    raise BrowserError("no browser window could be bound")


def _elements(cfg: dict, window: dict, query: str | None = None) -> dict:
    """Read the page. Returns only the compact view (pair still minted first)."""
    _, _, sem = _bind_and_read(cfg, window, query)
    return sem


def _compact(sem: dict, cfg: dict) -> dict:
    """A bounded, model-readable view of a semantic snapshot."""
    refs = []
    for ref in (sem.get("refs") or [])[: int(cfg.get("maxElements", 60))]:
        item = {"ref": ref.get("ref"), "role": ref.get("role"),
                "name": ref.get("name")}
        if ref.get("value") is not None:
            item["value"] = ref["value"]
        if ref.get("actions"):
            item["actions"] = ref["actions"]
        refs.append(item)
    page = sem.get("page") or {}
    return {
        "title": page.get("title"),
        "url": page.get("url"),
        "outline": (sem.get("outline") or "")[:2000],
        "elements": refs,
    }


# --- public API -----------------------------------------------------------


def browser_read(goal: str | None = None, cfg: dict | None = None) -> dict:
    """Read the browser page as DOM elements (no OCR). Read-only.

    With `goal`, a semantic query narrows the result (e.g. "the search box").
    Read-only, so it is allowed while the panic freeze is set.
    """
    cfg = cfg or load_config()
    started = time.monotonic()

    def run(window):
        sem = _elements(cfg, window, query=goal)
        view = _compact(sem, cfg)
        return {
            **view,
            "pid": window.get("pid"),
            "debug_port": window.get("debug_port"),
            "query": goal,
            "element_count": len(view["elements"]),
            "elapsed_s": round(time.monotonic() - started, 2),
        }

    return _with_browser(cfg, run)


def browser_click(goal: str, cfg: dict | None = None,
                  route: str | None = None) -> dict:
    """Click a page element found by `goal` (role/name), then verify.

    Uses the `dom_event` route measured to work here: a synthetic background DOM
    click that never moves the real mouse. The result is verified by re-reading
    the page (url/title) rather than trusting the dispatch, which the driver
    itself reports as `unverifiable`.
    """
    cfg = cfg or load_config()
    panic.guard("a browser click")
    started = time.monotonic()

    def run(window):
        target, tab, sem = _bind_and_read(cfg, window, query=goal)
        view = _compact(sem, cfg)
        match = select_element(view["elements"], goal, cfg, CLICK_ACTIONS)
        if match is None:
            raise BrowserError(
                f"no page element matched {goal!r}; "
                f"{len(view['elements'])} element(s) were visible"
            )
        ref = match["ref"]
        url_before = view.get("url")
        title_before = view.get("title")

        result = _run_driver(cfg, "browser_click", {
            "session": str(cfg.get("session") or "qwen"),
            "target_id": target, "tab_id": tab, "ref": ref,
            "input_route": route or cfg.get("clickRoute") or "dom_event",
        })

        # Verify by re-reading: dispatch success is not activation.
        time.sleep(0.8)
        after = _compact(_elements(cfg, window), cfg)
        changed = (after.get("url") != url_before
                   or after.get("title") != title_before)
        out = {
            "clicked": match,
            "route": result.get("route") or (route or cfg.get("clickRoute")),
            "url_before": url_before,
            "url_after": after.get("url"),
            "title_after": after.get("title"),
            "verified": changed,
            "verification": "satisfied" if changed else "unsatisfied",
            "verification_reason": (
                f"page changed to {after.get('title')!r}" if changed
                else "the page did not visibly change"
            ),
            "elapsed_s": round(time.monotonic() - started, 2),
        }
        if cfg.get("audit", True):
            from . import audit
            audit.record({
                "tool": "browser_click", "app": "browser", "window": title_before,
                "goal": goal, "outcome": out["verification"],
                "verified": changed, "reason": out["verification_reason"],
                "cost_usd": match.get("cost_usd") or 0.0,
                "takeover": "background",
                "selection": match.get("selection"),
                "confidence": match.get("confidence"),
            })
        return out

    return _with_browser(cfg, run)


def browser_type(text: str, goal: str | None = None, cfg: dict | None = None,
                 replace: bool = False, submit: bool = False) -> dict:
    """Type text into a page field (found by `goal`), optionally submitting.

    HARD RULE: never used for passwords, payment or authentication fields; the
    caller's policy check refuses sensitive text before this is reached.
    """
    cfg = cfg or load_config()
    panic.guard("browser typing")
    started = time.monotonic()

    def run(window):
        target, tab, sem = _bind_and_read(cfg, window, query=goal)
        view = _compact(sem, cfg)
        if goal:
            match = select_element(view["elements"], goal, cfg, TYPE_ACTIONS)
            if match is None:
                raise BrowserError(f"no page field matched {goal!r}")
        else:
            match = _first_input(view["elements"])
            if match is None:
                raise BrowserError("no text field is visible on the page")
        ref = match["ref"]

        result = _run_driver(cfg, "browser_type", {
            "session": str(cfg.get("session") or "qwen"),
            "target_id": target, "tab_id": tab, "ref": ref, "text": str(text),
            "replace": bool(replace),
        })
        delivered = int(((result.get("delivery") or {}).get("delivered_count")) or 0)

        out = {
            "field": match,
            "delivered_count": delivered,
            "route": result.get("route"),
            "replaced": bool(replace),
            # Typing is confirmed by the driver's delivered count, not a page
            # diff: the field value is not always re-readable, so report honestly.
            "verified": delivered > 0,
            "verification": "satisfied" if delivered else "unknown",
            "verification_reason": (
                f"{delivered} character(s) delivered into {match.get('name') or 'the field'}"
                if delivered else "the driver delivered no characters"
            ),
            "elapsed_s": round(time.monotonic() - started, 2),
        }
        if cfg.get("audit", True):
            from . import audit
            audit.record({
                "tool": "browser_type", "app": "browser",
                "window": view.get("title"), "goal": f"type into {match.get('name')}",
                "outcome": "satisfied" if delivered else "unknown",
                "verified": delivered > 0, "reason": f"{delivered} chars delivered",
                "cost_usd": 0.0, "takeover": "background",
            })
        return out

    return _with_browser(cfg, run)


def browser_navigate(url: str, cfg: dict | None = None) -> dict:
    """Navigate the bound tab to an http(s)/about URL and verify."""
    cfg = cfg or load_config()
    panic.guard("a browser navigation")
    url = (url or "").strip()
    if not re.match(r"^(https?|about):", url, re.IGNORECASE):
        raise BrowserError("only http:, https: and about: URLs can be opened here")
    started = time.monotonic()

    def run(window):
        target, tab = _pair(cfg, window)
        _run_driver(cfg, "browser_navigate", {
            "session": str(cfg.get("session") or "qwen"),
            "target_id": target, "tab_id": tab, "url": url,
        })
        time.sleep(0.8)
        after = _compact(_elements(cfg, window), cfg)
        return {
            "url_requested": url,
            "url_after": after.get("url"),
            "title_after": after.get("title"),
            "verified": bool(after.get("url")),
            "elapsed_s": round(time.monotonic() - started, 2),
        }

    return _with_browser(cfg, run)


def browser_search(query: str, cfg: dict | None = None) -> dict:
    """Goal-level action: run a web search and land on the results.

    This is roadmap #2 in miniature - one tool with a reliable outcome instead of
    exposing navigate/type/click to the model as separate steps. It composes the
    verified primitives and reports the outcome honestly:

        1. navigate to the search engine,
        2. type the query into the field (role-aware matching),
        3. activate the submit control and verify the page actually changed.

    Each step is verified, and the result says which step failed. It never
    guesses: if the field or the submit control cannot be identified, it says so
    rather than clicking something plausible.

    HARD RULE: the query is ordinary text. It is refused if it looks like a
    credential (see the caller's policy check), and password/payment fields are
    never targeted because this only ever fills the page's search box.
    """
    cfg = cfg or load_config()
    panic.guard("a web search")
    started = time.monotonic()
    query = (query or "").strip()
    if not query:
        raise BrowserError("a search query is required")
    if len(query) > 300:
        raise BrowserError("that search query is too long")

    engine = str(cfg.get("searchEngine") or "https://duckduckgo.com")
    steps: list[dict] = []

    nav = browser_navigate(engine, cfg)
    steps.append({"step": "navigate", "verified": nav.get("verified"),
                  "url": nav.get("url_after")})
    if not nav.get("verified"):
        return _search_failure("could not open the search engine", steps,
                               started, cfg, query)

    typed = browser_type(query, goal="search field", cfg=cfg)
    steps.append({"step": "type", "verified": typed.get("verified"),
                  "field": (typed.get("field") or {}).get("name")})
    if not typed.get("verified"):
        return _search_failure("could not type into the search box", steps,
                               started, cfg, query)

    clicked = browser_click("Search button", cfg=cfg)
    steps.append({"step": "submit", "verified": clicked.get("verified"),
                  "url": clicked.get("url_after")})
    if not clicked.get("verified"):
        return _search_failure("could not submit the search", steps,
                               started, cfg, query)

    out = {
        "query": query,
        "engine": engine,
        "results_url": clicked.get("url_after"),
        "results_title": clicked.get("title_after"),
        "verified": True,
        "verification": "satisfied",
        "verification_reason": "the search results page loaded",
        "steps": steps,
        "elapsed_s": round(time.monotonic() - started, 2),
    }
    if cfg.get("audit", True):
        from . import audit
        audit.record({
            "tool": "browser_search", "app": "browser",
            "window": clicked.get("title_after"), "goal": f"search: {query}",
            "outcome": "satisfied", "verified": True,
            "reason": out["verification_reason"],
            "cost_usd": 0.0, "takeover": "background",
        })
    return out


def _search_failure(why: str, steps: list[dict], started: float,
                    cfg: dict, query: str) -> dict:
    out = {
        "query": query,
        "verified": False,
        "verification": "unsatisfied",
        "verification_reason": why,
        "steps": steps,
        "elapsed_s": round(time.monotonic() - started, 2),
    }
    if cfg.get("audit", True):
        from . import audit
        audit.record({
            "tool": "browser_search", "app": "browser",
            "window": None, "goal": f"search: {query}",
            "outcome": "unsatisfied", "verified": False, "reason": why,
            "cost_usd": 0.0, "takeover": "background",
        })
    return out


def _first_input(elements: list[dict]) -> dict | None:
    for item in elements:
        if item.get("role") in ("textbox", "searchbox", "combobox", "input"):
            return item
    return None


# Actions that mean "this element can be clicked / typed into". A page element
# is not actionable merely because it has a name (Cua publishes both `refs` and
# non-actionable `content_refs`; the latter must never be treated as clickable).
CLICK_ACTIONS = ("click", "pointer")
TYPE_ACTIONS = ("type",)


def _actionable(elements: list[dict], actions: tuple[str, ...]) -> list[dict]:
    """Elements whose declared `actions` include one of `actions`.

    When the snapshot carries no per-element action information, keep everything
    rather than silently emptying the candidate list.
    """
    out = [e for e in elements
           if any(a in (e.get("actions") or []) for a in actions)]
    return out if out else [e for e in elements if e.get("actions") is not None] or elements


def _jev_select(elements: list[dict], goal: str, cfg: dict,
                actions: tuple[str, ...]) -> tuple[dict | None, str]:
    """One Jev Choice over the supplied refs. Returns (element, why).

    This is the same contract as the desktop path (`vision.select`): the model
    picks exactly one supplied id or "none", and code owns the confidence floor
    and the action. `actions` pre-filters to elements that actually declare the
    kind of interaction being asked for.
    """
    candidates = _actionable(elements, actions)
    criteria = {
        str(e.get("ref")): f'{e.get("role") or "element"} "{e.get("name") or ""}"'
        + (f' (actions: {", ".join(e.get("actions") or [])})' if e.get("actions") else "")
        for e in candidates
        if e.get("ref")
    }
    if not criteria:
        return None, "the page exposed no actionable elements"
    criteria["none"] = "No supplied element accomplishes the goal"
    state = json.dumps({
        "goal": goal,
        "page": {"title": None, "url": None},
        "elements": [
            {"id": str(e.get("ref")), "role": e.get("role"), "name": e.get("name"),
             "value": e.get("value"), "actions": e.get("actions")}
            for e in candidates if e.get("ref")
        ],
    }, ensure_ascii=False)
    instructions = (
        f"Goal: {goal}. Select exactly one element id from the supplied list "
        "that best accomplishes the goal, preferring an element whose actions "
        "match the intended interaction (a button to activate, a text field to "
        'type into). If none fits, choose "none".'
    )
    try:
        payload = selection.ask(state, criteria, instructions,
                                selection.jev_config(cfg))
    except triage.TriageError as exc:
        raise BrowserError(f"selection model unavailable: {exc}")
    by_ref = {str(e.get("ref")): e for e in candidates if e.get("ref")}
    try:
        choice = selection.choose(payload, set(by_ref), cfg)
    except selection.SelectionError as exc:
        return None, str(exc)
    element = dict(by_ref[choice.id])
    element["confidence"] = choice.confidence
    element["cost_usd"] = choice.cost_usd
    return element, f"selected {choice.id} (confidence {choice.confidence:.2f})"


def _best_match(elements: list[dict], goal: str) -> dict | None:
    """Deterministic fallback used only when Jev is unavailable.

    Kept deliberately: selection must degrade rather than break, so a missing
    key or an outage falls back to this instead of failing the action. It is a
    hand-written re-rank, not a judgement - see `_jev_select` for the primary
    path. Measured: with the goal "Search", plain name overlap chose the
    combobox "Search with DuckDuckGo" over the "Search" button, which is exactly
    the kind of mistake the model is there to avoid.
    """
    raw = (goal or "").strip().lower()
    if not raw:
        return elements[0] if elements else None

    role_words = {"button": "button", "link": "link",
                  "field": "textinput", "box": "textinput",
                  "input": "textinput", "textbox": "textinput",
                  "searchbox": "textinput"}
    wanted_roles = {role for word, role in role_words.items() if word in raw}
    words = [w for w in re.findall(r"[a-z0-9]+", raw)
             if w not in ("the", "a", "an", "on", "in") and w not in role_words]

    # A combobox/searchbox is a text input as far as a spoken goal is concerned.
    text_roles = {"textbox", "combobox", "searchbox", "input"}

    def role_matches(role: str) -> bool:
        if wanted_roles == {"textinput"}:
            return role in text_roles
        return role in wanted_roles

    scored = []
    for item in elements:
        name = str(item.get("name") or "").lower()
        role = str(item.get("role") or "").lower()
        score = 0
        # An exact name match on the name portion is the strongest signal.
        if name and name == " ".join(words):
            score += 10
        elif needle_words_in(name, words):
            score += 6
        if raw and raw in name:
            score += 4
        score += sum(2 for w in words if w in name)
        if wanted_roles and role_matches(role):
            score += 3
        elif wanted_roles:
            # The goal named a role and this element is not it: penalise so a
            # textbox is not chosen when a button was asked for.
            score -= 2
        if score > 0:
            scored.append((score, item))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


def select_element(elements: list[dict], goal: str, cfg: dict,
                   actions: tuple[str, ...] = CLICK_ACTIONS) -> dict | None:
    """Pick the element that fulfils `goal`, or None. Jev first, fallback second.

    Returns a copy of the chosen element with `confidence`, `cost_usd` and
    `selection` (how it was chosen) attached, so the caller can report honestly
    and the audit log can record whether a model or the fallback chose it.

    Selection must *degrade*, not break: a missing key or a Jev outage falls back
    to the deterministic scorer rather than failing the action. The fallback is
    labelled so the difference is never hidden.
    """
    if not elements:
        return None

    pool = _actionable(elements, actions)
    used_fallback = False
    why = ""
    chosen = None

    if cfg.get("selectWithJev", True):
        ok, reason = selection.available(cfg)
        if ok:
            chosen, why = _jev_select(elements, goal, cfg, actions)
        else:
            used_fallback = True
            why = f"selection model unavailable ({reason})"
    else:
        used_fallback = True
        why = "Jev selection disabled (selectWithJev=false)"

    if chosen is None:
        # Either Jev was unavailable, or it found no fit. Fall back only when the
        # model could not run at all; a confident "none" is a real answer and
        # must not be overridden by a string match.
        if used_fallback:
            chosen = _best_match(pool, goal)
        if chosen is None:
            return None

    out = dict(chosen)
    out.setdefault("confidence", None)
    out.setdefault("cost_usd", 0.0)
    out["selection"] = "fallback" if used_fallback else "jev"
    out["selection_reason"] = why
    return out



def needle_words_in(name: str, words: list[str]) -> bool:
    """True when every goal word appears in the name, in any order."""
    return bool(words) and all(word in name for word in words)


__all__ = [
    "BrowserError", "available", "find_browser", "browser_read",
    "browser_click", "browser_type", "browser_navigate", "browser_search",
    "load_config", "select_element",
]
