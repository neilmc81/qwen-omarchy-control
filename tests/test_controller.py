"""Tests for the desktop controller argument handling and dispatch glue.

Hyprland JSON endpoints are mocked; real subprocess calls to hyprctl/omarchy/
wpctl are replaced, so these tests never touch a live desktop.
"""

import json
import unittest
from unittest import mock

from qwen_omarchy_control import desktop
from qwen_omarchy_control.desktop import DesktopController, DesktopError

WINDOWS = [
    {"address": "0xaaa", "class": "google-chrome", "title": "Google",
     "initialTitle": "Google", "workspace": {"name": "1"}, "pid": 10, "hidden": False},
    {"address": "0xbbb", "class": "org.omarchy.terminal", "title": "Omarchy",
     "initialTitle": "Omarchy", "workspace": {"name": "1"}, "pid": 11, "hidden": False},
    {"address": "0xccc", "class": "com.mitchellh.ghostty", "title": "hermes",
     "initialTitle": "hermes", "workspace": {"name": "2"}, "pid": 12, "hidden": False},
]

WORKSPACES = [
    {"id": 1, "name": "1", "monitor": "eDP-1", "windows": 2, "hasfullscreen": False},
    {"id": 2, "name": "2", "monitor": "eDP-1", "windows": 1, "hasfullscreen": False},
]


class FakeHypr:
    def __init__(self):
        self.active = dict(WINDOWS[1])
        self.clients = list(WINDOWS)

    def hyprctl_json(self, *args):
        if args[0] == "clients":
            return self.clients
        if args[0] == "workspaces":
            return WORKSPACES
        if args[0] == "activewindow":
            return self.active
        if args[0] == "monitors":
            return [{"name": "eDP-1", "width": 1366, "height": 768}]
        return None


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.ctrl = DesktopController()
        self.fake = FakeHypr()
        self.patchers = [
            mock.patch.object(desktop, "hyprctl_json", self.fake.hyprctl_json),
            mock.patch.object(desktop, "run", return_value=(0, "ok")),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def test_list_windows(self):
        rows = self.ctrl.list_windows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["class"], "google-chrome")

    def test_out_of_range_workspace(self):
        with self.assertRaises(DesktopError):
            self.ctrl.switch_workspace(0)
        with self.assertRaises(DesktopError):
            self.ctrl.switch_workspace(101)

    def test_focus_window_uses_address(self):
        self.ctrl.focus_window("chrome")
        cmd = desktop.run.call_args.args[0]
        self.assertEqual(cmd[0], "hyprctl")
        self.assertIn("0xaaa", cmd[2])

    def test_focus_unknown(self):
        with self.assertRaises(DesktopError):
            self.ctrl.focus_window("nothing-matches-this")

    def test_set_volume_limits(self):
        with self.assertRaises(DesktopError):
            self.ctrl.set_volume(-1)
        with self.assertRaises(DesktopError):
            self.ctrl.set_volume(150)
        self.ctrl.set_volume(50)
        argv = desktop.run.call_args.args[0]
        self.assertEqual(argv[-1], "50%")


if __name__ == "__main__":
    unittest.main()