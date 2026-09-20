"""Tests for typed browser control (cua-driver CDP binding, no OCR).

All offline: the cua-driver subprocess is mocked, so these pin the parsing,
matching, ref-validity contract, safety guard and failure modes without a
browser or a network call.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qwen_omarchy_control import browser, panic


def sem_snapshot(refs, url="https://example.com/", title="Example Domain"):
    return {
        "status": "ok",
        "target_id": "bt-1",
        "tabs": [{"tab_id": "tab-1", "title": title, "url": url}],
        "page": {"title": title, "url": url},
        "refs": refs,
        "outline": "- link \"Learn more\"",
    }


LINK = {"ref": "p1:0", "role": "link", "name": "Learn more",
        "actions": ["click", "pointer"]}


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = browser.CONFIG_FILE
        browser.CONFIG_FILE = Path(self._tmp.name) / "browser.json"

    def tearDown(self):
        browser.CONFIG_FILE = self._old
        self._tmp.cleanup()

    def test_enabled_by_default(self):
        self.assertTrue(browser.load_config()["enabled"])

    def test_click_route_defaults_to_dom_event(self):
        # Measured on this Hyprland setup: the default `trusted` route is
        # refused (route_unavailable); dom_event works and never moves the mouse.
        self.assertEqual(browser.load_config()["clickRoute"], "dom_event")

    def test_kill_switch(self):
        browser.CONFIG_FILE.write_text(json.dumps({"enabled": False}))
        ok, reason = browser.available()
        self.assertFalse(ok)
        self.assertIn("disabled", reason)


class BrowserMatchTest(unittest.TestCase):
    def test_exact_name_beats_role_match(self):
        elements = [
            {"ref": "a", "role": "button", "name": "Submit"},
            {"ref": "b", "role": "link", "name": "Learn more about us"},
        ]
        match = browser._best_match(elements, "learn more")
        self.assertEqual(match["ref"], "b")

    def test_no_plausible_match_returns_none(self):
        elements = [{"ref": "a", "role": "heading", "name": "Welcome"}]
        self.assertIsNone(browser._best_match(elements, "the Save button"))

    def test_role_word_disambiguates_button_from_field(self):
        # Measured on DuckDuckGo: goal "Search" matched the combobox
        # "Search with DuckDuckGo" (longer name wins raw overlap) instead of the
        # "Search" button, so "click Search" filled the field instead of
        # submitting. Naming the role must win.
        elements = [
            {"ref": "box", "role": "combobox",
             "name": "Search with DuckDuckGo", "value": "q"},
            {"ref": "btn", "role": "button", "name": "Search"},
        ]
        self.assertEqual(browser._best_match(elements, "Search")["ref"], "btn")
        self.assertEqual(
            browser._best_match(elements, "Search button")["ref"], "btn")
        self.assertEqual(
            browser._best_match(elements, "the search field")["ref"], "box")
        self.assertEqual(
            browser._best_match(elements, "the search box")["ref"], "box")

    def test_empty_goal_takes_first(self):
        elements = [{"ref": "a", "role": "link", "name": "One"}]
        self.assertEqual(browser._best_match(elements, "")["ref"], "a")

    def test_first_input_finds_textbox(self):
        elements = [{"ref": "a", "role": "heading", "name": "Hi"},
                    {"ref": "b", "role": "combobox", "name": "Search"}]
        self.assertEqual(browser._first_input(elements)["ref"], "b")

    def test_first_input_none_when_absent(self):
        self.assertIsNone(browser._first_input([{"ref": "a", "role": "link"}]))


class AvailabilityTest(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(browser.DEFAULT_CONFIG)

    def _patch(self, payloads):
        queue = list(payloads)

        def fake_run(argv, **kwargs):
            proc = mock.Mock()
            proc.returncode = 0
            proc.stdout = queue.pop(0) if queue else "{}"
            proc.stderr = ""
            return proc

        return mock.patch("subprocess.run", side_effect=fake_run)

    def test_no_debug_port_is_a_clear_error(self):
        windows = json.dumps({"windows": [
            {"pid": 9, "window_id": 2, "title": "X", "app_name": "google-chrome",
             "is_on_screen": True}]})
        with self._patch([windows]), \
                mock.patch.object(browser, "_debug_port_for", return_value=None):
            with self.assertRaises(browser.BrowserError) as ctx:
                browser.find_browser(self.cfg)
        message = str(ctx.exception)
        self.assertIn("remote-debugging-port", message)
        self.assertIn("OCR", message)

    def test_no_browser_open_is_a_clear_error(self):
        windows = json.dumps({"windows": [
            {"pid": 9, "window_id": 2, "title": "term", "app_name": "foot",
             "is_on_screen": True}]})
        with self._patch([windows]):
            with self.assertRaises(browser.BrowserError) as ctx:
                browser.find_browser(self.cfg)
        self.assertIn("no browser window", str(ctx.exception))

    def test_browser_with_port_is_found(self):
        windows = json.dumps({"windows": [
            {"pid": 9, "window_id": 2, "title": "X", "app_name": "google-chrome",
             "is_on_screen": True}]})
        with self._patch([windows]), \
                mock.patch.object(browser, "_debug_port_for", return_value=9222):
            window = browser.find_browser(self.cfg)
        self.assertEqual(window["debug_port"], 9222)

    def test_refusal_is_a_clean_error(self):
        payload = json.dumps({"refusal": {"code": "browser_consent_required"}})
        with self._patch([payload]):
            with self.assertRaises(browser.BrowserError) as ctx:
                browser._run_driver(self.cfg, "browser_prepare", {})
        self.assertIn("refused", str(ctx.exception))


class BrowserActionTest(unittest.TestCase):
    """The ref-validity contract: mint the pair, then act without re-reading."""

    def setUp(self):
        self.cfg = dict(browser.DEFAULT_CONFIG)
        self._tmp = tempfile.TemporaryDirectory()
        # browser_click/type append to the audit log; point it at a scratch file
        # so the suite never writes to the user's real trajectory.
        from qwen_omarchy_control import audit
        self._old_audit = audit.LOG_FILE
        audit.LOG_FILE = Path(self._tmp.name) / "trajectory.jsonl"

    def tearDown(self):
        from qwen_omarchy_control import audit
        audit.LOG_FILE = self._old_audit
        self._tmp.cleanup()

    def _driver(self, calls):
        """Return a _run_driver replacement that records calls and answers."""
        def fake(cfg, tool, args):
            calls.append((tool, args))
            if tool == "get_browser_state" and "target_id" not in args:
                # binding read -> pair
                return {"status": "ok", "target_id": "bt-1",
                        "tabs": [{"tab_id": "tab-1"}]}
            if tool == "get_browser_state":
                return sem_snapshot([LINK])
            if tool == "browser_click":
                return {"route": "dom", "effect": "unverifiable"}
            if tool == "browser_type":
                return {"route": "trusted_input",
                        "delivery": {"delivered_count": 5}}
            if tool == "browser_prepare":
                return {"status": "ok"}
            return {"status": "ok"}
        return fake

    def test_click_reuses_the_ref_minted_pair(self):
        calls = []
        with mock.patch.object(browser, "_bind_candidates",
                               return_value=[{"pid": 9, "window_id": 2,
                                              "debug_port": 9222}]), \
                mock.patch.object(browser, "_run_driver",
                                  side_effect=self._driver(calls)), \
                mock.patch.object(browser, "_debug_port_for", return_value=9222), \
                mock.patch("time.sleep"):
            result = browser.browser_click("Learn more", cfg=self.cfg)
        click = [a for (t, a) in calls if t == "browser_click"]
        self.assertEqual(len(click), 1)
        # The click must use the same target/tab the ref was minted against.
        self.assertEqual(click[0]["target_id"], "bt-1")
        self.assertEqual(click[0]["tab_id"], "tab-1")
        self.assertEqual(click[0]["ref"], "p1:0")
        self.assertEqual(click[0]["input_route"], "dom_event")
        self.assertEqual(result["verification"], "unsatisfied")

    def test_click_verifies_a_url_change(self):
        calls = []
        seq = {"n": 0}

        def fake(cfg, tool, args):
            calls.append((tool, args))
            if tool == "get_browser_state" and "target_id" not in args:
                return {"status": "ok", "target_id": "bt-1",
                        "tabs": [{"tab_id": "tab-1"}]}
            if tool == "get_browser_state":
                seq["n"] += 1
                if seq["n"] == 1:
                    return sem_snapshot([LINK])
                return sem_snapshot([], url="https://www.iana.org/",
                                    title="Example Domains")
            if tool == "browser_click":
                return {"route": "dom"}
            return {"status": "ok"}

        with mock.patch.object(browser, "_bind_candidates",
                               return_value=[{"pid": 9, "window_id": 2}]), \
                mock.patch.object(browser, "_run_driver", side_effect=fake), \
                mock.patch.object(browser, "_debug_port_for", return_value=9222), \
                mock.patch("time.sleep"):
            result = browser.browser_click("Learn more", cfg=self.cfg)
        self.assertTrue(result["verified"])
        self.assertEqual(result["verification"], "satisfied")
        self.assertIn("iana.org", result["url_after"])

    def test_type_reports_delivered_count(self):
        calls = []
        search = {"ref": "p2:0", "role": "combobox", "name": "Search with DuckDuckGo"}

        def fake(cfg, tool, args):
            calls.append((tool, args))
            if tool == "get_browser_state" and "target_id" not in args:
                return {"status": "ok", "target_id": "bt-1",
                        "tabs": [{"tab_id": "tab-1"}]}
            if tool == "get_browser_state":
                return sem_snapshot([search])
            if tool == "browser_type":
                return {"route": "trusted_input",
                        "delivery": {"delivered_count": 5}}
            return {"status": "ok"}

        with mock.patch.object(browser, "_bind_candidates",
                               return_value=[{"pid": 9, "window_id": 2}]), \
                mock.patch.object(browser, "_run_driver", side_effect=fake), \
                mock.patch("time.sleep"):
            result = browser.browser_type("hello", goal="search", cfg=self.cfg)
        self.assertEqual(result["delivered_count"], 5)
        self.assertEqual(result["field"]["ref"], "p2:0")

    def test_navigate_rejects_non_http(self):
        with self.assertRaises(browser.BrowserError):
            browser.browser_navigate("file:///etc/passwd", cfg=self.cfg)


class BrowserSafetyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = panic.PANIC_FILE
        panic.PANIC_FILE = Path(self._tmp.name) / "stop"
        self.cfg = dict(browser.DEFAULT_CONFIG)

    def tearDown(self):
        panic.PANIC_FILE = self._old
        self._tmp.cleanup()

    def test_freeze_blocks_click_type_and_navigate(self):
        panic.set_panic()
        for call in (lambda: browser.browser_click("x", cfg=self.cfg),
                     lambda: browser.browser_type("x", cfg=self.cfg),
                     lambda: browser.browser_navigate("https://x.test", cfg=self.cfg)):
            with self.assertRaises(panic.PanicError):
                call()

    def test_freeze_does_not_block_read(self):
        # Looking is always allowed; only acting is frozen.
        panic.set_panic()
        with mock.patch.object(browser, "_bind_candidates",
                               return_value=[{"pid": 9, "window_id": 2}]), \
                mock.patch.object(browser, "_run_driver",
                                  side_effect=lambda c, t, a: (
                                      {"status": "ok", "target_id": "bt-1",
                                       "tabs": [{"tab_id": "tab-1"}]}
                                      if "target_id" not in a
                                      else sem_snapshot([LINK]))):
            result = browser.browser_read(cfg=self.cfg)
        self.assertEqual(result["title"], "Example Domain")

    def test_no_pointer_movement_api_is_exposed(self):
        # The browser path is DOM-only on purpose: it must not offer a
        # coordinate-click, which would move the real mouse.
        source = (Path(browser.__file__).read_text())
        self.assertNotIn("pointer_move", source)


class BrowserSearchTest(unittest.TestCase):
    """The goal-level action (#2): compose verified steps, report honestly."""

    def setUp(self):
        self.cfg = dict(browser.DEFAULT_CONFIG, audit=False)
        self._tmp = tempfile.TemporaryDirectory()
        from qwen_omarchy_control import audit
        self._old_audit = audit.LOG_FILE
        audit.LOG_FILE = Path(self._tmp.name) / "trajectory.jsonl"

    def tearDown(self):
        from qwen_omarchy_control import audit
        audit.LOG_FILE = self._old_audit
        self._tmp.cleanup()

    def test_rejects_empty_query(self):
        with self.assertRaises(browser.BrowserError):
            browser.browser_search("", cfg=self.cfg)

    def test_rejects_overlong_query(self):
        with self.assertRaises(browser.BrowserError):
            browser.browser_search("x" * 500, cfg=self.cfg)

    def test_composes_and_verifies_all_steps(self):
        with mock.patch.object(browser, "browser_navigate",
                               return_value={"verified": True,
                                             "url_after": "https://ddg.test/"}), \
                mock.patch.object(browser, "browser_type",
                                  return_value={"verified": True,
                                                "field": {"name": "Search"}}), \
                mock.patch.object(browser, "browser_click",
                                  return_value={"verified": True,
                                                "url_after": "https://ddg.test/?q=x",
                                                "title_after": "x at DuckDuckGo"}):
            result = browser.browser_search("x", cfg=self.cfg)
        self.assertTrue(result["verified"])
        self.assertEqual([s["step"] for s in result["steps"]],
                         ["navigate", "type", "submit"])

    def test_stops_and_reports_which_step_failed(self):
        # The whole point of a goal-level action: never claim success if a step
        # did not verify, and say which one.
        with mock.patch.object(browser, "browser_navigate",
                               return_value={"verified": True}), \
                mock.patch.object(browser, "browser_type",
                                  return_value={"verified": False}), \
                mock.patch.object(browser, "browser_click") as click:
            result = browser.browser_search("x", cfg=self.cfg)
        self.assertFalse(result["verified"])
        self.assertIn("search box", result["verification_reason"])
        self.assertEqual([s["step"] for s in result["steps"]],
                         ["navigate", "type"])
        click.assert_not_called()

    def test_failed_navigation_does_not_type(self):
        with mock.patch.object(browser, "browser_navigate",
                               return_value={"verified": False}), \
                mock.patch.object(browser, "browser_type") as typed:
            result = browser.browser_search("x", cfg=self.cfg)
        self.assertFalse(result["verified"])
        self.assertIn("search engine", result["verification_reason"])
        typed.assert_not_called()


class MultiWindowTest(unittest.TestCase):
    """The typed path is a single-window capability; say so, do not guess.

    Measured on this machine: with one top-level browser window the driver binds
    `exact`; with two it binds `heuristic` (title-only) and refuses every element
    read and mutation with `authorization_host_failed: ... mutations require an
    exact bounds- or cardinality-correlated binding`. Returning an empty page
    instead of an honest error would be the worst outcome, so this pins the error.
    """

    def setUp(self):
        self.cfg = dict(browser.DEFAULT_CONFIG)

    def test_two_windows_same_pid_is_reported_not_guessed(self):
        windows = json.dumps({"windows": [
            {"pid": 9, "window_id": 2, "title": "A", "app_name": "google-chrome",
             "is_on_screen": True},
            {"pid": 9, "window_id": 3, "title": "B", "app_name": "google-chrome",
             "is_on_screen": True}]})

        def fake_run(argv, **kwargs):
            proc = mock.Mock(returncode=0, stdout=windows, stderr="")
            return proc

        with mock.patch("subprocess.run", side_effect=fake_run), \
                mock.patch.object(browser, "_debug_port_for", return_value=9222):
            with self.assertRaises(browser.BrowserError) as ctx:
                browser._with_browser(self.cfg, lambda w: w)
        message = str(ctx.exception)
        self.assertIn("more than one window", message)
        self.assertIn("read_screen", message)
        # The error is read by the voice model; it must not invite a
        # destructive workaround (observed: it offered to close a window).
        self.assertIn("Do NOT close", message)

    def test_single_window_is_not_flagged(self):
        windows = json.dumps({"windows": [
            {"pid": 9, "window_id": 2, "title": "A", "app_name": "google-chrome",
             "is_on_screen": True}]})

        def fake_run(argv, **kwargs):
            return mock.Mock(returncode=0, stdout=windows, stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run), \
                mock.patch.object(browser, "_debug_port_for", return_value=9222), \
                mock.patch.object(browser, "_prepare"):
            result = browser._with_browser(self.cfg, lambda w: w)
        self.assertEqual(result["window_id"], 2)


class BrowserAuditTest(unittest.TestCase):
    def test_click_is_recorded_in_the_audit_log(self):
        from qwen_omarchy_control import audit
        tmp = tempfile.TemporaryDirectory()
        old = audit.LOG_FILE
        audit.LOG_FILE = Path(tmp.name) / "trajectory.jsonl"
        try:
            cfg = dict(browser.DEFAULT_CONFIG)
            with mock.patch.object(browser, "_bind_candidates",
                                   return_value=[{"pid": 9, "window_id": 2}]), \
                    mock.patch.object(browser, "_run_driver",
                                      side_effect=lambda c, t, a: (
                                          {"status": "ok", "target_id": "bt-1",
                                           "tabs": [{"tab_id": "tab-1"}]}
                                          if t == "get_browser_state" and "target_id" not in a
                                          else (sem_snapshot([LINK])
                                                if t == "get_browser_state"
                                                else {"route": "dom"}))), \
                    mock.patch("time.sleep"):
                browser.browser_click("Learn more", cfg=cfg)
            rows = audit.read_log()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["tool"], "browser_click")
            self.assertEqual(rows[0]["takeover"], "background")
        finally:
            audit.LOG_FILE = old
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
