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

    def test_opencode_alias_resolves_to_agent_window(self):
        self.fake.clients = list(WINDOWS) + [{
            "address": "0xddd", "class": "org.omarchy.agent",
            "title": "OC | Fix the failing tests", "initialTitle": "",
            "workspace": {"name": "1"}, "pid": 99, "hidden": False,
        }]
        got = self.ctrl.focus_window("opencode")
        self.assertEqual(got["class"], "org.omarchy.agent")
        # also by the "coding agent" alias
        got2 = self.ctrl.focus_window("coding agent")
        self.assertEqual(got2["address"], "0xddd")

    def test_type_text_uses_wtype_and_enter(self):
        self.ctrl.type_text("hermes", "hello there", send=True)
        cmds = [c.args[0] for c in desktop.run.call_args_list]
        wtype_calls = [c for c in cmds if c[0] == "wtype"]
        self.assertEqual(wtype_calls[0], ["wtype", "hello there"])
        self.assertEqual(wtype_calls[1], ["wtype", "-k", "Return"])

    def test_type_text_rejects_sensitive(self):
        with self.assertRaises(DesktopError):
            self.ctrl.type_text("terminal", "my password is hunter2")

    def test_launch_agent_builds_visible_hermes_argv(self):
        with mock.patch("subprocess.Popen") as popen:
            popen.return_value = mock.Mock(pid=42)
            self.ctrl.launch_agent("hermes", "build a todo app")
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], "foot")
        self.assertIn("--app-id=qwen-hermes", argv)
        self.assertIn("-H", argv)
        # Omarchy's hermes default-agent flags: chat --yolo --tui --query=...
        self.assertIn("--yolo", argv)
        self.assertIn("--tui", argv)
        self.assertTrue(any(a.startswith("--query=") and "build a todo app" in a for a in argv))

    def test_launch_agent_rejects_unknown_and_sensitive(self):
        with self.assertRaises(DesktopError):
            self.ctrl.launch_agent("bogus", "hi")
        with self.assertRaises(DesktopError):
            self.ctrl.launch_agent("hermes", "delete with my password 1234")

    def test_launch_agent_empty_prompt_opens_fresh_no_query(self):
        with mock.patch("subprocess.Popen") as popen:
            popen.return_value = mock.Mock(pid=43)
            self.ctrl.launch_agent("hermes", "")
        argv = popen.call_args.args[0]
        # fresh open = hermes --yolo, NO --query, NO chat subcommand
        self.assertIn("hermes", argv)
        self.assertIn("--yolo", argv)
        self.assertNotIn("--query=", [a for a in argv if a.startswith("--query=")])
        self.assertNotIn("chat", argv)

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