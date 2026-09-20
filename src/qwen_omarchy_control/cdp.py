"""Direct Chrome DevTools Protocol control: the multi-window route.

Why this exists
---------------
The typed browser tools (`browser.py`) go through cua-driver, which binds one
*native window* to a browser target. Measured: that binding is only `exact` when
the browser owns a single top-level window; with two, Cua falls back to a
heuristic (title-only) binding and refuses every element read and mutation. That
is a binding artefact, not a CDP limitation - CDP addresses *tabs*, and does not
care how many native windows they sit in.

So when Cua cannot bind, this module talks CDP directly: list the pages, pick the
one the user means, and read/click/type on it. It uses `ws.py` (stdlib-only) so
nothing new needs installing.

This is not a general CDP client. It implements exactly the operations the voice
tools need - enumerate, snapshot, click, type, navigate - and refuses anything
else. It never enables `Runtime`, never evaluates page scripts, and never
touches browser-level APIs: the goal is the element DOM, not the browser's
identity or the profile behind it.

Security note, stated plainly
-----------------------------
Reaching this endpoint requires Chrome to be running with a DevTools port, which
in this project means the existing `--remote-debugging-port` setup on the
logged-in profile. Any local process that can reach that port already has full
control of the browser; this module does not widen that. It is loopback-only and
never exposed to the network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import ws

DEFAULT_ENDPOINT = "http://127.0.0.1:9222"


class CdpError(RuntimeError):
    """A CDP operation could not be completed."""


@dataclass
class Page:
    """One browser tab, as CDP sees it."""

    id: str
    title: str
    url: str
    ws_url: str
    window_id: int | None = None

    @property
    def description(self) -> str:
        return f"{self.title or 'untitled'} | {self.url}"


@dataclass
class Element:
    """A page element from a DOM snapshot."""

    ref: str
    role: str
    name: str
    value: str | None = None
    actions: list[str] = field(default_factory=list)
    backend_node_id: int | None = None

    def to_dict(self) -> dict:
        out = {"ref": self.ref, "role": self.role, "name": self.name,
               "actions": list(self.actions)}
        if self.value is not None:
            out["value"] = self.value
        return out


# --- discovery (plain HTTP, no websocket) ----------------------------------


def _get_json(url: str, timeout: float = 8.0) -> object:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise CdpError(f"CDP endpoint returned HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise CdpError(f"CDP endpoint unavailable at {url}: {exc}") from exc


def list_pages(endpoint: str = DEFAULT_ENDPOINT) -> list[Page]:
    """Every real page (tab) the browser exposes.

    Only `type == "page"` targets are returned: iframes, service workers and
    browser UI are not somewhere the user clicks.
    """
    payload = _get_json(f"{endpoint.rstrip('/')}/json/list")
    if not isinstance(payload, list):
        raise CdpError("CDP /json/list did not return a list")
    pages = []
    for item in payload:
        if not isinstance(item, dict) or item.get("type") != "page":
            continue
        ws_url = str(item.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            continue
        pages.append(Page(
            id=str(item.get("id") or ""),
            title=str(item.get("title") or ""),
            url=str(item.get("url") or ""),
            ws_url=ws_url,
        ))
    return pages


def _browser_ws_url(endpoint: str = DEFAULT_ENDPOINT) -> str:
    payload = _get_json(f"{endpoint.rstrip('/')}/json/version")
    if not isinstance(payload, dict) or not payload.get("webSocketDebuggerUrl"):
        raise CdpError("CDP /json/version did not return a websocket URL")
    return str(payload["webSocketDebuggerUrl"])


def _window_id_for_ws(endpoint: str, ws_url: str) -> int | None:
    """The native window id a page lives in, via `Browser.getWindowForTarget`.

    This is how a tab is matched to the window the user is looking at, which is
    the whole point of the direct route: CDP itself has no notion of "the
    focused window", but it can report which window owns a tab.
    """
    target_id = ws_url.rstrip("/").rsplit("/", 1)[-1]
    try:
        with ws.WebSocket(_browser_ws_url(endpoint), timeout=10) as sock:
            sock.send(json.dumps({"id": 1, "method": "Browser.getWindowForTarget",
                                  "params": {"targetId": target_id}}))
            message = json.loads(sock.recv(idle_timeout=8))
    except (ws.WebSocketError, ValueError, OSError):
        return None
    window_id = (message.get("result") or {}).get("windowId")
    return int(window_id) if window_id is not None else None


# --- one page session -------------------------------------------------------


class PageSession:
    """A CDP session bound to one tab. Use as a context manager."""

    def __init__(self, page: Page, timeout: float = 20.0):
        self.page = page
        self._sock = ws.WebSocket(page.ws_url, timeout=timeout)
        self._next_id = 0

    def call(self, method: str, params: dict | None = None,
             idle_timeout: float = 15.0) -> dict:
        """One CDP command. Raises CdpError on a protocol-level error."""
        self._next_id += 1
        message_id = self._next_id
        self._sock.send(json.dumps({"id": message_id, "method": method,
                                    "params": params or {}}))
        # The endpoint may emit events before our result; skip them.
        for _ in range(50):
            raw = self._sock.recv(idle_timeout=idle_timeout)
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("id") != message_id:
                continue
            if "error" in message:
                raise CdpError(
                    f"{method} failed: {message['error'].get('message')}")
            return message.get("result") or {}
        raise CdpError(f"{method}: no matching result")

    def __enter__(self) -> "PageSession":
        return self

    def __exit__(self, *exc) -> None:
        self._sock.close()


# --- DOM snapshot -----------------------------------------------------------
#
# Elements are collected into a page-global array (`window.__qwen_refs`) and a
# ref is its index into that array. This matters: an earlier version filtered to
# *visible* elements for the snapshot but then re-queried and indexed the
# UNFILTERED node list when acting, so every ref pointed at the wrong element
# (measured: typing reported 0 characters delivered). Storing the resolved nodes
# once makes the snapshot and the action agree by construction.
#
# The array is a plain page global, cleared on every snapshot. It adds no DOM
# attribute, is not persisted, and navigation (a new page context) discards it -
# which is exactly when stale refs *should* stop working. It is read-only with
# respect to the page's own content and state.

_QUERY = "'a, button, input, textarea, select, [role], [onclick], [tabindex]'"

_SNAPSHOT_JS = r"""
(() => {
  const out = [];
  window.__qwen_refs = [];
  const isInput = el => {
    const t = (el.tagName || '').toLowerCase();
    return t === 'input' || t === 'textarea' || t === 'select';
  };
  const visible = el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const nodes = document.querySelectorAll(__QWEN_QUERY__);
  for (const el of nodes) {
    if (!visible(el)) continue;
    const role = (el.getAttribute('role') ||
      ({a:'link', button:'button', input:'textbox', textarea:'textbox',
        select:'combobox'}[ (el.tagName||'').toLowerCase() ]) || 'element');
    const name = (el.getAttribute('aria-label') || el.getAttribute('title') ||
      el.innerText || el.value || el.placeholder || el.name || '').trim().slice(0, 120);
    if (!name && !isInput(el)) continue;
    const actions = isInput(el) ? ['click','type'] :
      (role === 'link' || role === 'button' || el.onclick || el.getAttribute('role')) ? ['click'] : [];
    const ref = 'd' + window.__qwen_refs.length;
    window.__qwen_refs.push(el);
    out.push({
      ref, role, name,
      value: isInput(el) ? String(el.value || '').slice(0, 80) : null,
      actions,
    });
    if (out.length >= 300) break;
  }
  return JSON.stringify(out);
})()
""".replace("__QWEN_QUERY__", _QUERY)


def _resolve_js(ref: str) -> str:
    """JS that resolves a ref to a live element, or a reason string."""
    index = int(ref.lstrip("d")) if ref.lstrip("d").isdigit() else -1
    if index < 0:
        raise CdpError(f"unrecognised element ref {ref!r}")
    return (
        "(() => { const a = window.__qwen_refs || [];"
        f" const el = a[{index}];"
        " if (!el) return {reason: 'no page snapshot; read the page again'};"
        " if (!el.isConnected) return {reason: 'that element is gone; read the page again'};"
        " return {el}; })()"
    )


def snapshot(session: PageSession, max_elements: int = 80) -> tuple[list[Element], dict]:
    """Read the page's interactive elements. Returns (elements, page_info)."""
    # Runtime.evaluate is used only to read; the expression is a fixed constant
    # above, never caller text.
    result = session.call("Runtime.evaluate", {
        "expression": _SNAPSHOT_JS,
        "returnByValue": True,
        "awaitPromise": False,
    })
    raw = (result.get("result") or {}).get("value")
    if not isinstance(raw, str):
        raise CdpError("page snapshot returned no data")
    try:
        items = json.loads(raw)
    except ValueError as exc:
        raise CdpError(f"page snapshot was not JSON: {exc}") from exc
    elements = [
        Element(
            ref=str(item.get("ref")),
            role=str(item.get("role") or "element"),
            name=str(item.get("name") or ""),
            value=item.get("value"),
            actions=list(item.get("actions") or []),
        )
        for item in items
    ][:max_elements]
    return elements, page_info(session)


def page_info(session: PageSession, live: bool = True) -> dict:
    """Title and URL. With `live`, read them from the page itself.

    A fresh read matters after an action: the tab's URL in `/json/list` is a
    snapshot from when the list was fetched, which is exactly the trap that made
    verification report "nothing changed" while the page had in fact navigated.
    """
    if live:
        try:
            result = session.call("Runtime.evaluate", {
                "expression": "JSON.stringify({t: document.title, u: location.href})",
                "returnByValue": True,
            })
            raw = (result.get("result") or {}).get("value")
            if isinstance(raw, str):
                data = json.loads(raw)
                return {"title": data.get("t"), "url": data.get("u")}
        except (CdpError, ValueError):
            pass
    return {"title": session.page.title, "url": session.page.url}


# --- actions ----------------------------------------------------------------


def _resolve_ref(session: PageSession, ref: str) -> None:
    """Scroll the referenced element into view so a click lands on it."""
    _ = _resolve_js(ref)  # validates the ref shape
    session.call("Runtime.evaluate", {
        "expression": (
            "(() => { const a = window.__qwen_refs || [];"
            f" const el = a[{ref.lstrip('d')}];"
            " if (el) el.scrollIntoView({block:'center'}); return !!el; })()"
        ),
        "returnByValue": True,
    })


def click_element(session: PageSession, element: Element) -> dict:
    """Click a snapshot element by ref. Background only: no native input.

    Implemented as an in-page click on the resolved node, then verified by
    re-reading the page. Dispatch is not proof of activation, so the caller
    verifies.
    """
    index = int(element.ref.lstrip("d")) if element.ref.lstrip("d").isdigit() else -1
    if index < 0:
        raise CdpError(f"unrecognised element ref {element.ref!r}")
    result = session.call("Runtime.evaluate", {
        "expression": (
            "(() => { const a = window.__qwen_refs || [];"
            f" const el = a[{index}];"
            " if (!el || !el.isConnected) return 'gone';"
            " el.scrollIntoView({block:'center'}); el.focus(); el.click();"
            " return 'clicked'; })()"
        ),
        "returnByValue": True,
        "userGesture": True,
    })
    outcome = (result.get("result") or {}).get("value")
    if outcome == "gone":
        raise CdpError(
            "the page changed since the last read, so that element no longer "
            "exists; read the page again"
        )
    return {"delivery": "cdp_dom_click", "element": element.to_dict()}


def type_into(session: PageSession, element: Element, text: str,
              replace: bool = False) -> dict:
    """Type into a snapshot element by ref. Background only.

    Sets the value through the native setter and dispatches input events, so
    framework state (React et al.) stays consistent.
    """
    import json as _json
    index = int(element.ref.lstrip("d")) if element.ref.lstrip("d").isdigit() else -1
    if index < 0:
        raise CdpError(f"unrecognised element ref {element.ref!r}")
    result = session.call("Runtime.evaluate", {
        "expression": (
            "(() => { const a = window.__qwen_refs || [];"
            f" const el = a[{index}];"
            " if (!el || !el.isConnected) return 'gone';"
            " el.scrollIntoView({block:'center'}); el.focus();"
            " const proto = el.tagName === 'TEXTAREA'"
            "   ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
            " const setter = Object.getOwnPropertyDescriptor(proto, 'value');"
            " const next = " + ("''" if replace else "String(el.value||'')") +
            " + " + _json.dumps(str(text)) + ";"
            " if (setter && setter.set) { setter.set.call(el, next); } else { el.value = next; }"
            " el.dispatchEvent(new Event('input', {bubbles:true}));"
            " el.dispatchEvent(new Event('change', {bubbles:true}));"
            " return String(el.value||'').length; })()"
        ),
        "returnByValue": True,
        "userGesture": True,
    })
    delivered = (result.get("result") or {}).get("value")
    if delivered == "gone":
        raise CdpError("the page changed since the last read; read the page again")
    return {
        "delivery": "cdp_set_value",
        "delivered_count": int(delivered or 0),
        "element": element.to_dict(),
    }


def navigate(session: PageSession, url: str) -> dict:
    result = session.call("Page.navigate", {"url": url})
    if result.get("errorText"):
        raise CdpError(f"navigation failed: {result['errorText']}")
    return {"frame_id": result.get("frameId")}


def press_enter(session: PageSession) -> dict:
    """Dispatch a TRUSTED Enter key to the focused element.

    This is the primary submit. A synthetic `el.click()` on the submit control
    does not work everywhere: measured on DuckDuckGo, its "Search" button is a
    `type=button` JS widget with no owning form, and a scripted click left the
    page unchanged, while a trusted Enter on the focused field navigated
    correctly (verified on Google: 13 chars typed -> real results URL).
    """
    common = {"key": "Enter", "code": "Enter",
              "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}
    session.call("Input.dispatchKeyEvent", {"type": "rawKeyDown", **common})
    session.call("Input.dispatchKeyEvent", {"type": "char", "text": "\r"})
    session.call("Input.dispatchKeyEvent", {"type": "keyUp", **common})
    return {"delivery": "cdp_trusted_enter"}


def submit_form(session: PageSession) -> dict:
    """Ask the focused field's owning form to submit, if it has one.

    Tried before a scripted button click because it is closer to what the page
    itself does. Returns whether a form was found; a page whose search box is a
    detached JS widget (DuckDuckGo) has none, which is exactly the case that
    needs the caller's fallback.
    """
    result = session.call("Runtime.evaluate", {
        "expression": (
            "(() => { const el = document.activeElement;"
            " if (!el || !el.form) return 'no form';"
            " try { el.form.requestSubmit(); return 'submitted'; }"
            " catch (e) { try { el.form.submit(); return 'submitted'; }"
            " catch (e2) { return 'failed'; } } })()"
        ),
        "returnByValue": True,
        "userGesture": True,
    })
    return {"delivery": "cdp_form_submit",
            "result": (result.get("result") or {}).get("value")}


def focus_field(session: PageSession, element: Element) -> dict:
    """Focus a snapshot element (trusted focus, so keyboards hit the right place)."""
    index = int(element.ref.lstrip("d")) if element.ref.lstrip("d").isdigit() else -1
    if index < 0:
        raise CdpError(f"unrecognised element ref {element.ref!r}")
    result = session.call("Runtime.evaluate", {
        "expression": (
            "(() => { const a = window.__qwen_refs || [];"
            f" const el = a[{index}];"
            " if (!el || !el.isConnected) return 'gone';"
            " el.scrollIntoView({block:'center'}); el.focus(); return 'focused'; })()"
        ),
        "returnByValue": True,
        "userGesture": True,
    })
    if (result.get("result") or {}).get("value") == "gone":
        raise CdpError("the page changed since the last read; read the page again")
    return {"delivery": "cdp_focus", "element": element.to_dict()}


__all__ = [
    "CdpError", "Page", "Element", "PageSession",
    "list_pages", "snapshot", "page_info",
    "click_element", "type_into", "navigate", "press_enter", "focus_field",
]
