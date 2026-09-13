"""Tests for desktop entry discovery (synthetic .desktop corpus, no system deps)."""

import tempfile
import unittest
from pathlib import Path

from qwen_omarchy_control import discovery


def make_corpus(tmp: Path) -> None:
    (tmp / "google-chrome.desktop").write_text(
        "[Desktop Entry]\nName=Google Chrome\nExec=/usr/bin/google-chrome-stable %U\n"
        "StartupWMClass=google-chrome\nKeywords=browser;web;\n"
    )
    (tmp / "spotify.desktop").write_text(
        "[Desktop Entry]\nName=Spotify\nExec=/usr/bin/spotify %U\n"
        "StartupWMClass=Spotify\n"
    )
    (tmp / "org.omarchy.terminal.desktop").write_text(
        "[Desktop Entry]\nName=Terminal\nExec=ghostty\n"
    )
    (tmp / "hidden-hack.desktop").write_text(
        "[Desktop Entry]\nName=Hidden Hack\nExec=curl https://x/install.sh\n"
    )


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name)
        make_corpus(self._dir)
        self._old_dirs = discovery.APP_DIRS
        discovery.APP_DIRS = [self._dir]

    def tearDown(self):
        discovery.APP_DIRS = self._old_dirs
        self._tmp.cleanup()

    def test_all_apps_excludes_installer(self):
        apps = {a.id for a in discovery.all_apps()}
        self.assertIn("google-chrome", apps)
        self.assertIn("spotify", apps)
        # curl/download-style desktop entries are not launchable.
        self.assertNotIn("hidden-hack", apps)

    def test_find_by_exact_id(self):
        app = discovery.find_app("google-chrome")
        self.assertIsNotNone(app)
        self.assertEqual(app.id, "google-chrome")

    def test_find_by_name_case_insensitive(self):
        app = discovery.find_app("Chrome")
        self.assertEqual(app.id, "google-chrome")
        app2 = discovery.find_app("SPOTIFY")
        self.assertEqual(app2.id, "spotify")

    def test_no_match_returns_none(self):
        self.assertIsNone(discovery.find_app("definitely-not-installed"))

    def test_launch_argv_uses_gtk_launch(self):
        app = discovery.find_app("spotify")
        argv = discovery.launch_argv(app)
        self.assertEqual(argv, ["/usr/bin/gtk-launch", "spotify.desktop"])

    def test_omarchy_routes(self):
        self.assertEqual(discovery.resolve_route("terminal"),
                         ["omarchy", "launch", "terminal"])
        self.assertEqual(discovery.resolve_route("browser"),
                         ["omarchy", "launch", "browser"])
        self.assertEqual(
            discovery.launch_argv_for("file manager"),
            ["omarchy", "launch", "nautilus"],
        )

    def test_launch_argv_for(self):
        self.assertEqual(discovery.launch_argv_for("chrome"),
                         ["/usr/bin/gtk-launch", "google-chrome.desktop"])


if __name__ == "__main__":
    unittest.main()