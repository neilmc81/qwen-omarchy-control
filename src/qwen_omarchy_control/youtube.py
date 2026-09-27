"""Grounded controls for the single selected YouTube watch-page player.

All actions use the existing loopback CDP connection. They never send desktop
input or move the user's pointer. A page action is only successful after the
player's own state has been read back.
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse

from . import browser, cdp, panic


_STATE_JS = r"""
(() => {
  const player = document.querySelector('#movie_player');
  const video = player && player.querySelector('video.html5-main-video');
  if (!video) return JSON.stringify({available:false});
  const button = selector => player.querySelector(selector);
  const captions = button('.ytp-subtitles-button');
  const theater = button('.ytp-size-button');
  const autoplay = button('.ytp-autonav-toggle-button');
  return JSON.stringify({
    available:true, paused:video.paused, ended:video.ended,
    current_time:video.currentTime,
    duration:Number.isFinite(video.duration) ? video.duration : null,
    volume:Math.round(video.volume * 100), muted:video.muted,
    playback_rate:video.playbackRate,
    fullscreen:!!document.fullscreenElement,
    captions:captions ? captions.getAttribute('aria-pressed') === 'true' : null,
    theater:theater ? !!document.querySelector('ytd-watch-flexy[theater]') : null,
    autoplay:autoplay ? autoplay.getAttribute('aria-checked') === 'true' : null,
    next_available:!!button('.ytp-next-button:not([aria-disabled="true"])'),
    video_id:new URL(location.href).searchParams.get('v'),
    loaded_video_id:typeof player.getVideoData === 'function' ?
      player.getVideoData().video_id : null,
    title:document.title, url:location.href
  });
})()
"""

_ACTION_JS = r"""
(async () => {
  const player = document.querySelector('#movie_player');
  const video = player && player.querySelector('video.html5-main-video');
  if (!video) return 'no player';
  const action = __ACTION__, amount = __AMOUNT__;
  const button = selector => player.querySelector(selector);
  const click = selector => {
    const el = button(selector);
    if (!el || el.disabled || el.getAttribute('aria-disabled') === 'true')
      return false;
    el.click(); return true;
  };
  switch (action) {
    case 'play':
      if (!video.paused) return 'already';
      try { await video.play(); return 'sent'; } catch (e) { return 'play refused'; }
    case 'pause':
      if (video.paused) return 'already';
      video.pause(); return 'sent';
    case 'seek_forward': case 'seek_backward': case 'seek_to': case 'restart': {
      if (!Number.isFinite(video.duration)) return 'unseekable';
      const target = action === 'restart' ? 0 :
        action === 'seek_to' ? amount :
        video.currentTime + (action === 'seek_forward' ? amount : -amount);
      video.currentTime = Math.max(0, Math.min(video.duration, target));
      return 'sent';
    }
    case 'volume_up': case 'volume_down': case 'volume_set': {
      const target = action === 'volume_set' ? amount / 100 :
        video.volume + (action === 'volume_up' ? amount : -amount) / 100;
      video.volume = Math.max(0, Math.min(1, target));
      if (action !== 'volume_down' && video.volume > 0) video.muted = false;
      return 'sent';
    }
    case 'mute': video.muted = true; return 'sent';
    case 'unmute': video.muted = false; return 'sent';
    case 'speed': video.playbackRate = amount; return 'sent';
    case 'fullscreen_on':
      if (document.fullscreenElement) return 'already';
      return click('.ytp-fullscreen-button') ? 'sent' : 'control unavailable';
    case 'fullscreen_off':
      if (!document.fullscreenElement) return 'already';
      try { await document.exitFullscreen(); return 'sent'; }
      catch (e) { return 'fullscreen refused'; }
    case 'captions_on': case 'captions_off': {
      const el = button('.ytp-subtitles-button');
      if (!el) return 'control unavailable';
      if ((el.getAttribute('aria-pressed') === 'true') === (action === 'captions_on'))
        return 'already';
      return click('.ytp-subtitles-button') ? 'sent' : 'control unavailable';
    }
    case 'theater_on': case 'theater_off': {
      const el = button('.ytp-size-button');
      if (!el) return 'control unavailable';
      if (!!document.querySelector('ytd-watch-flexy[theater]') === (action === 'theater_on'))
        return 'already';
      return click('.ytp-size-button') ? 'sent' : 'control unavailable';
    }
    case 'autoplay_on': case 'autoplay_off': {
      const el = button('.ytp-autonav-toggle-button');
      if (!el) return 'control unavailable';
      if ((el.getAttribute('aria-checked') === 'true') === (action === 'autoplay_on'))
        return 'already';
      return click('.ytp-autonav-toggle-button') ? 'sent' : 'control unavailable';
    }
    case 'next':
      return click('.ytp-next-button') ? 'sent' : 'control unavailable';
  }
  return 'unsupported action';
})()
"""

_ACTIONS = {
    "play", "pause", "seek_forward", "seek_backward", "seek_to", "restart",
    "volume_up", "volume_down", "volume_set", "mute", "unmute", "speed",
    "fullscreen_on", "fullscreen_off", "captions_on", "captions_off",
    "theater_on", "theater_off", "autoplay_on", "autoplay_off", "next",
}


def _watch_id(url: str) -> str | None:
    if not browser._is_youtube(url):
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.path != "/watch":
        return None
    return urllib.parse.parse_qs(parsed.query).get("v", [None])[0]


def _watch_page(cfg: dict, page_title: str | None) -> cdp.Page:
    pages = [p for p in cdp.list_pages(browser._cdp_endpoint(cfg)) if _watch_id(p.url)]
    if page_title:
        matches = [p for p in pages if p.title.casefold().strip() == page_title.casefold().strip()]
        if len(matches) != 1:
            raise browser.BrowserError("the exact title did not identify one YouTube watch tab")
        return matches[0]
    if len(pages) == 1:
        return pages[0]
    if not pages:
        raise browser.BrowserError("no YouTube watch tab is open")
    try:
        focused = browser._cdp_pick_page(cfg)
    except browser.BrowserError:
        focused = None
    if focused and any(p.id == focused.id for p in pages):
        return focused
    raise browser.BrowserError("multiple YouTube watch tabs are open; focus one or supply its exact page title")


def _evaluate(session: cdp.PageSession, expression: str, *, action: bool = False):
    result = session.call("Runtime.evaluate", {
        "expression": expression, "returnByValue": True,
        "awaitPromise": action, "userGesture": action,
    })
    if result.get("exceptionDetails"):
        raise browser.BrowserError("the YouTube player rejected the browser action")
    return (result.get("result") or {}).get("value")


def _state(session: cdp.PageSession) -> dict:
    raw = _evaluate(session, _STATE_JS)
    if not isinstance(raw, str):
        raise browser.BrowserError("the YouTube player state was unreadable")
    state = json.loads(raw)
    if not state.get("available") or not _watch_id(state.get("url") or ""):
        raise browser.BrowserError("the selected tab has no YouTube watch-page player")
    return state


def player_state(page_title: str | None = None, cfg: dict | None = None) -> dict:
    """Read the uniquely selected player's real media state, without input."""
    cfg = dict(cfg or browser.load_config())
    with cdp.PageSession(_watch_page(cfg, page_title)) as session:
        return _state(session)


def _amount(action: str, amount: float | None) -> float:
    if amount is None:
        if action in {"seek_to", "volume_set", "speed"}:
            raise browser.BrowserError(f"{action} requires an amount")
        amount = 10 if action.startswith(("seek_", "volume_")) else 1
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount):
        raise browser.BrowserError("amount must be a finite number")
    amount = float(amount)
    if action in {"seek_forward", "seek_backward"} and not 1 <= amount <= 600:
        raise browser.BrowserError("seek step must be 1–600 seconds")
    if action == "seek_to" and not 0 <= amount <= 86400:
        raise browser.BrowserError("seek position must be 0–86400 seconds")
    if action in {"volume_up", "volume_down"} and not 1 <= amount <= 100:
        raise browser.BrowserError("volume step must be 1–100 percent")
    if action == "volume_set" and not 0 <= amount <= 100:
        raise browser.BrowserError("volume must be 0–100 percent")
    if action == "speed" and not 0.25 <= amount <= 2:
        raise browser.BrowserError("playback speed must be 0.25–2")
    if action not in {"seek_forward", "seek_backward", "seek_to", "volume_up",
                      "volume_down", "volume_set", "speed"} and amount != 1:
        raise browser.BrowserError("this action does not take an amount")
    return amount


def _verified(action: str, before: dict, after: dict, amount: float) -> bool:
    if action == "play":
        return not after["paused"] and (not before["paused"] or
                                        after["current_time"] > before["current_time"])
    if action == "pause":
        return after["paused"]
    if action in {"mute", "unmute"}:
        return after["muted"] == (action == "mute")
    if action.startswith("fullscreen_"):
        return after["fullscreen"] == (action == "fullscreen_on")
    for prefix, key in (("captions_", "captions"), ("theater_", "theater"),
                        ("autoplay_", "autoplay")):
        if action.startswith(prefix):
            return after[key] is not None and after[key] == action.endswith("_on")
    if action == "next":
        return (after["video_id"] != before["video_id"] and
                after.get("loaded_video_id") == after["video_id"])
    if action in {"volume_up", "volume_down", "volume_set"}:
        expected = (amount if action == "volume_set" else
                    max(0, min(100, before["volume"] +
                               (amount if action == "volume_up" else -amount))))
        return abs(after["volume"] - expected) <= 1
    if action == "speed":
        return abs(after["playback_rate"] - amount) < 0.01
    duration = before["duration"]
    if duration is None:
        return False
    target = (0 if action == "restart" else amount if action == "seek_to" else
              before["current_time"] + (amount if action == "seek_forward" else -amount))
    target = max(0, min(duration, target))
    tolerance = 2 if not after["paused"] else 1
    return abs(after["current_time"] - target) <= tolerance


def player_control(action: str, amount: float | None = None,
                   page_title: str | None = None, cfg: dict | None = None) -> dict:
    """Act on one YouTube player, then verify its media/URL state."""
    cfg = dict(cfg or browser.load_config())
    if action not in _ACTIONS:
        raise browser.BrowserError("unsupported YouTube player action")
    amount = _amount(action, amount)
    panic.guard("YouTube player control")
    page = _watch_page(cfg, page_title)
    with cdp.PageSession(page) as session:
        before = _state(session)
        if (before["video_id"] != _watch_id(page.url) or
                before.get("loaded_video_id") != before["video_id"]):
            raise browser.BrowserError("the YouTube tab changed before the action")
        expression = _ACTION_JS.replace("__ACTION__", json.dumps(action)).replace(
            "__AMOUNT__", json.dumps(amount))
        outcome = _evaluate(session, expression, action=True)
        if outcome not in {"sent", "already"}:
            return {"verified": False, "action": action, "reason": outcome,
                    "state": before, "route_used": "cdp_direct"}
        after = before
        for _ in range(15 if action in {"play", "next"} else 6):
            time.sleep(0.2)
            try:
                after = _state(session)
            except browser.BrowserError:
                if action == "next":
                    continue
                raise
            if _verified(action, before, after, amount):
                break
    verified = _verified(action, before, after, amount)
    return {"verified": verified, "action": action, "amount": amount,
            "reason": "player state confirmed" if verified else "player state did not confirm the action",
            "state": after, "route_used": "cdp_direct"}
