"""Tests for the direct-CDP route: the WebSocket codec and the CDP layer.

All offline: the socket and Chrome's HTTP endpoints are mocked, so these pin the
framing, the ref registry contract and the failure modes without a browser.
"""

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qwen_omarchy_control import browser, cdp, ws


class FrameCodecTest(unittest.TestCase):
    """RFC 6455 framing is the part that is easy to get subtly wrong."""

    def test_short_text_frame_round_trips(self):
        frame = ws.encode_frame(b"hello", ws.OP_TEXT, mask=False)
        opcode, payload, used = ws.decode_frame(frame)
        self.assertEqual(opcode, ws.OP_TEXT)
        self.assertEqual(payload, b"hello")
        self.assertEqual(used, len(frame))

    def test_medium_frame_uses_16_bit_length(self):
        body = b"x" * 300
        frame = ws.encode_frame(body, mask=False)
        self.assertEqual(frame[1] & 0x7F, 126)
        opcode, payload, _ = ws.decode_frame(frame)
        self.assertEqual(payload, body)

    def test_large_frame_uses_64_bit_length(self):
        body = b"y" * 70000
        frame = ws.encode_frame(body, mask=False)
        self.assertEqual(frame[1] & 0x7F, 127)
        _, payload, _ = ws.decode_frame(frame)
        self.assertEqual(len(payload), 70000)

    def test_client_frames_are_masked(self):
        # Chrome closes the connection on an unmasked client frame.
        frame = ws.encode_frame(b"hi", ws.OP_TEXT, mask=True)
        self.assertTrue(frame[1] & 0x80)
        # The mask key is present and the payload is obfuscated.
        self.assertNotEqual(frame[6:], b"hi")
        # Decoding a client frame must be refused: servers do not mask.
        with self.assertRaises(ws.WebSocketError):
            ws.decode_frame(frame)

    def test_mask_round_trips_through_a_manual_decode(self):
        # Simulate what a server does: unmask and read.
        frame = ws.encode_frame(b"payload", ws.OP_TEXT, mask=True)
        key = frame[2:6]
        body = frame[6:]
        unmasked = bytes(b ^ key[i % 4] for i, b in enumerate(body))
        self.assertEqual(unmasked, b"payload")

    def test_truncated_payload_is_reported(self):
        frame = ws.encode_frame(b"hello", mask=False)
        with self.assertRaises(ws.WebSocketError) as ctx:
            ws.decode_frame(frame[:-2])
        self.assertIn("truncated", str(ctx.exception))

    def test_fragmented_frame_is_refused(self):
        # FIN bit clear: not supported, and refused rather than half-read.
        frame = bytearray(ws.encode_frame(b"x", mask=False))
        frame[0] &= ~0x80
        with self.assertRaises(ws.WebSocketError) as ctx:
            ws.decode_frame(bytes(frame))
        self.assertIn("fragmented", str(ctx.exception))

    def test_non_ws_scheme_is_refused(self):
        with self.assertRaises(ws.WebSocketError):
            ws.WebSocket("https://example.com")


class CdpDiscoveryTest(unittest.TestCase):
    def _list_payload(self):
        return json.dumps([
            {"type": "page", "id": "1", "title": "Home", "url": "https://a.test/",
             "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/1"},
            {"type": "iframe", "id": "2", "title": "ad", "url": "https://ad.test/",
             "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/2"},
            {"type": "page", "id": "3", "title": "Docs", "url": "https://b.test/",
             "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/3"},
        ])

    def test_only_real_pages_are_returned(self):
        with mock.patch.object(cdp, "_get_json", return_value=json.loads(self._list_payload())):
            pages = cdp.list_pages()
        # The iframe is not somewhere a user clicks.
        self.assertEqual(len(pages), 2)
        self.assertEqual([p.title for p in pages], ["Home", "Docs"])

    def test_pages_without_a_ws_url_are_skipped(self):
        items = json.loads(self._list_payload())
        items[0].pop("webSocketDebuggerUrl")
        with mock.patch.object(cdp, "_get_json", return_value=items):
            pages = cdp.list_pages()
        self.assertEqual(len(pages), 1)

    def test_unreachable_endpoint_is_a_clean_error(self):
        import urllib.error
        with mock.patch.object(cdp.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(cdp.CdpError) as ctx:
                cdp.list_pages()
        self.assertIn("unavailable", str(ctx.exception))


class CdpSessionTest(unittest.TestCase):
    """The request/response pairing and event-skipping behaviour."""

    def _session(self, replies):
        page = cdp.Page(id="1", title="T", url="https://x.test/",
                        ws_url="ws://127.0.0.1:9222/devtools/page/1")
        session = cdp.PageSession.__new__(cdp.PageSession)
        session.page = page
        session._next_id = 0
        session._sock = mock.Mock()
        session._sock.recv.side_effect = replies
        return session

    def test_events_before_the_result_are_skipped(self):
        session = self._session([
            json.dumps({"method": "Page.loadEventFired", "params": {}}),
            json.dumps({"id": 1, "result": {"ok": True}}),
        ])
        self.assertEqual(session.call("Page.enable"), {"ok": True})

    def test_protocol_error_is_raised(self):
        session = self._session([
            json.dumps({"id": 1, "error": {"message": "no such node"}}),
        ])
        with self.assertRaises(cdp.CdpError) as ctx:
            session.call("DOM.describeNode")
        self.assertIn("no such node", str(ctx.exception))

    def test_missing_result_is_reported(self):
        # 60 unrelated messages: the loop gives up rather than hanging forever.
        session = self._session([
            json.dumps({"method": "tick", "params": {}}) for _ in range(60)
        ])
        with self.assertRaises(cdp.CdpError) as ctx:
            session.call("Thing.do")
        self.assertIn("no matching result", str(ctx.exception))


class CdpSnapshotTest(unittest.TestCase):
    def test_snapshot_parses_elements_and_page_info(self):
        page = cdp.Page(id="1", title="T", url="https://x.test/",
                        ws_url="ws://127.0.0.1:9222/devtools/page/1")
        session = cdp.PageSession.__new__(cdp.PageSession)
        session.page = page
        session._next_id = 0
        session._sock = mock.Mock()
        elements = [{"ref": "d0", "role": "button", "name": "Save",
                     "value": None, "actions": ["click"]}]
        session._sock.recv.side_effect = [
            json.dumps({"id": 1, "result": {"result": {"value": json.dumps(elements)}}}),
            json.dumps({"id": 2, "result": {"result": {
                "value": json.dumps({"t": "T", "u": "https://x.test/"})}}}),
        ]
        got, info = cdp.snapshot(session)
        self.assertEqual(got[0].ref, "d0")
        self.assertEqual(got[0].actions, ["click"])
        self.assertEqual(info["url"], "https://x.test/")

    def test_non_json_snapshot_is_reported(self):
        page = cdp.Page(id="1", title="T", url="u", ws_url="ws://x/1")
        session = cdp.PageSession.__new__(cdp.PageSession)
        session.page = page
        session._next_id = 0
        session._sock = mock.Mock()
        session._sock.recv.side_effect = [
            json.dumps({"id": 1, "result": {"result": {"value": "not json"}}}),
        ]
        with self.assertRaises(cdp.CdpError):
            cdp.snapshot(session)

    def test_bad_ref_shape_is_refused(self):
        page = cdp.Page(id="1", title="T", url="u", ws_url="ws://x/1")
        session = cdp.PageSession.__new__(cdp.PageSession)
        session.page = page
        session._next_id = 0
        session._sock = mock.Mock()
        with self.assertRaises(cdp.CdpError):
            cdp.click_element(session, cdp.Element(ref="zzz", role="button", name="x"))


class CdpRoutingTest(unittest.TestCase):
    """The browser tools must use the CDP route only when needed."""

    def setUp(self):
        self.cfg = dict(browser.DEFAULT_CONFIG, audit=False)

    def test_multi_window_uses_cdp(self):
        with mock.patch.object(browser, "_multi_window", return_value=True), \
                mock.patch.object(browser, "_cdp_read",
                                  return_value={"title": "X", "elements": [],
                                                "element_count": 0,
                                                "route_used": "cdp_direct"}):
            out = browser.browser_read(cfg=self.cfg)
        self.assertEqual(out["route_used"], "cdp_direct")

    def test_single_window_does_not_use_cdp(self):
        with mock.patch.object(browser, "_multi_window", return_value=False), \
                mock.patch.object(browser, "_cdp_read") as cdp_read, \
                mock.patch.object(browser, "_bind_candidates",
                                  return_value=[{"pid": 1, "window_id": 2}]), \
                mock.patch.object(browser, "_prepare"), \
                mock.patch.object(browser, "_bind_and_read",
                                  return_value=("t", "b", {"refs": [], "page": {}})):
            browser.browser_read(cfg=self.cfg)
        cdp_read.assert_not_called()

    def test_cdp_can_be_disabled(self):
        cfg = dict(self.cfg, useCdpFallback=False)
        with mock.patch.object(browser, "_multi_window", return_value=True), \
                mock.patch.object(browser, "_bind_candidates", return_value=[]), \
                mock.patch.object(browser, "_prepare"):
            # Falls through to the Cua path, which reports the honest error.
            with self.assertRaises(browser.BrowserError):
                browser.browser_read(cfg=cfg)

    def test_search_uses_the_url_template_by_default(self):
        with mock.patch.object(browser, "browser_navigate",
                               return_value={"url_after": "https://ddg.test/?q=a+b",
                                             "title_after": "a b at DDG"}) as nav:
            out = browser.browser_search("a b", cfg=self.cfg)
        self.assertTrue(out["verified"])
        self.assertIn("a+b", nav.call_args[0][0])
        self.assertIn("q=", nav.call_args[0][0])

    def test_search_template_encodes_special_characters(self):
        with mock.patch.object(browser, "browser_navigate",
                               return_value={"url_after": "https://ddg.test/?q=x"}) as nav:
            browser.browser_search("c++ & rust", cfg=self.cfg)
        # '+' and '&' must be encoded, not left to break the query string.
        self.assertIn("c%2B%2B", nav.call_args[0][0])
        self.assertIn("%26", nav.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
