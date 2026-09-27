"""YouTube player targeting and state-based verification."""

import unittest
from unittest import mock

from qwen_omarchy_control import browser, cdp, youtube


WATCH = cdp.Page("watch", "A video - YouTube",
                 "https://www.youtube.com/watch?v=one", "ws://watch")


class PlayerTest(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(browser.DEFAULT_CONFIG, audit=False)
        self.state = {"available": True, "paused": True, "ended": False,
                      "current_time": 30.0, "duration": 100.0, "volume": 50,
                      "muted": False, "playback_rate": 1.0, "fullscreen": False,
                      "captions": False, "theater": False, "autoplay": False,
                      "video_id": "one", "loaded_video_id": "one", "url": WATCH.url}

    def test_unique_watch_tab_can_be_selected_among_other_tabs(self):
        other = cdp.Page("other", "Other", "https://other.test", "ws://other")
        with mock.patch.object(cdp, "list_pages", return_value=[other, WATCH]):
            self.assertEqual(youtube._watch_page(self.cfg, None), WATCH)

    def test_two_watch_tabs_refuse_without_target(self):
        other = cdp.Page("two", "Another - YouTube",
                         "https://www.youtube.com/watch?v=two", "ws://two")
        with mock.patch.object(cdp, "list_pages", return_value=[other, WATCH]), \
                mock.patch.object(browser, "_cdp_pick_page", side_effect=browser.BrowserError("ambiguous")):
            with self.assertRaisesRegex(browser.BrowserError, "multiple YouTube"):
                youtube._watch_page(self.cfg, None)
            self.assertEqual(youtube._watch_page(self.cfg, WATCH.title), WATCH)

    def test_other_sites_and_youtube_home_are_not_players(self):
        for url in ("https://notyoutube.com/watch?v=one", "https://www.youtube.com/"):
            self.assertIsNone(youtube._watch_id(url))

    def test_action_rechecks_live_watch_id_before_mutation(self):
        session = mock.MagicMock()
        session.__enter__.return_value = session
        stale = dict(self.state, video_id="two")
        with mock.patch.object(youtube, "_watch_page", return_value=WATCH), \
                mock.patch.object(cdp, "PageSession", return_value=session), \
                mock.patch.object(youtube, "_state", return_value=stale), \
                mock.patch.object(youtube, "_evaluate") as evaluate:
            with self.assertRaisesRegex(browser.BrowserError, "changed before"):
                youtube.player_control("pause", cfg=self.cfg)
        evaluate.assert_not_called()

    def test_play_is_verified_by_media_state_not_dispatch(self):
        session = mock.MagicMock()
        session.__enter__.return_value = session
        with mock.patch.object(youtube, "_watch_page", return_value=WATCH), \
                mock.patch.object(cdp, "PageSession", return_value=session), \
                mock.patch.object(youtube, "_state", side_effect=[self.state] +
                                  [self.state] * 15), \
                mock.patch.object(youtube, "_evaluate", return_value="sent"), \
                mock.patch.object(youtube.time, "sleep"):
            result = youtube.player_control("play", cfg=self.cfg)
        self.assertFalse(result["verified"])

    def test_seek_volume_fullscreen_and_next_postconditions(self):
        self.assertFalse(youtube._verified("play", self.state,
                                           dict(self.state, paused=False), 1))
        self.assertTrue(youtube._verified("play", self.state,
                                          dict(self.state, paused=False, current_time=31), 1))
        self.assertTrue(youtube._verified("seek_backward", self.state,
                                         dict(self.state, current_time=20), 10))
        self.assertTrue(youtube._verified("volume_down", self.state,
                                         dict(self.state, volume=40), 10))
        self.assertFalse(youtube._verified("fullscreen_on", self.state,
                                          self.state, 1))
        self.assertTrue(youtube._verified("next", self.state,
                                         dict(self.state, video_id="two", loaded_video_id="two"), 1))
        self.assertFalse(youtube._verified("next", self.state,
                                          dict(self.state, video_id="two"), 1))

    def test_invalid_amounts_refuse_before_input(self):
        for action, amount in (("seek_to", None), ("speed", 3),
                               ("volume_set", 110), ("seek_forward", -1)):
            with self.assertRaises(browser.BrowserError):
                youtube.player_control(action, amount, cfg=self.cfg)


if __name__ == "__main__":
    unittest.main()
