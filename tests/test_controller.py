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

    def test_pointer_move_builds_dispatch(self):
        desktop.run.return_value = (0, "10, 20")
        self.ctrl.pointer_move(300, 400)
        argv = desktop.run.call_args.args[0]
        self.assertEqual(argv, ["hyprctl", "dispatch",
                                'hl.dsp.cursor.move({ x = "300", y = "400" })'])

    def test_pointer_move_relative_offsets(self):
        desktop.run.return_value = (0, "10, 20")
        self.ctrl.pointer_move(5, -2, relative=True)
        argv = desktop.run.call_args.args[0]
        self.assertIn('x = "15"', argv[2])
        self.assertIn('y = "18"', argv[2])

    def test_mouse_click_without_daemon_errors(self):
        with mock.patch.object(desktop.Path, "exists", return_value=False):
            with self.assertRaises(DesktopError):
                self.ctrl.mouse_click()
            with self.assertRaises(DesktopError):
                self.ctrl.mouse_scroll("down")

    def test_mouse_click_builds_argv(self):
        with mock.patch.object(DesktopController, "_ydotool_ready", return_value=True):
            self.ctrl.mouse_click("right", double=True)
            argv = desktop.run.call_args.args[0]
            self.assertEqual(argv, ["ydotool", "click", "--repeat", "2", "0xC1"])

    def test_mouse_scroll_points_then_wheels(self):
        self.fake.active = {"address": "0xbbb", "class": "org.omarchy.terminal",
                            "at": [0, 0], "size": [800, 600]}
        with mock.patch.object(DesktopController, "_ydotool_ready", return_value=True), \
             mock.patch.object(desktop, "_dispatch") as dispatch:
            desktop.run.side_effect = [(0, "ok"), (0, "ok")]
            self.ctrl.mouse_scroll("up", pages=1)
        # First a pointer move to the active window centre...
        self.assertIn("cursor.move", dispatch.call_args.args[0])
        self.assertIn('x = "400"', dispatch.call_args.args[0])
        # ...then the wheel call.
        argv = desktop.run.call_args.args[0]
        self.assertEqual(argv[:3], ["ydotool", "mousemove", "--wheel"])
        self.assertEqual(argv[3], "-y")
        self.assertTrue(int(argv[4]) > 0)  # "up" is a positive wheel delta

    def test_describe_close_names_active_window(self):
        desc = self.ctrl.describe("close_active_window")
        self.assertIn("close", desc)
        self.assertIn("org.omarchy.terminal", desc)

    def test_describe_volume_and_move(self):
        self.assertEqual(self.ctrl.describe("set_volume", percent=42),
                         "set the volume to 42%")
        desc = self.ctrl.describe("move_active_window_to_workspace", number=3)
        self.assertIn("workspace 3", desc)
        self.assertIn("org.omarchy.terminal", desc)


class FindTextTest(unittest.TestCase):
    """OCR text -> screen coordinates, so OCR-only windows are clickable.

    Regression: read_screen returned words with no positions, so a model told
    to click something in Chrome guessed and clicked the wrong place.
    """

    def setUp(self):
        self.ctrl = DesktopController()

    def test_maps_image_box_to_screen_coords(self):
        # Full-resolution capture, so image px map 1:1 to screen px plus origin.
        words = [{"text": "Download", "left": 300, "top": 300,
                  "width": 120, "height": 30, "conf": 88.0}]
        with mock.patch.object(desktop, "hyprctl_json",
                               return_value=[{"x": 0, "y": 0, "width": 1366, "height": 768,
                                             "focused": True}]), \
                mock.patch.object(desktop, "run_bin",
                                  return_value=(0, b"png")), \
                mock.patch.object(desktop, "_ocr_words", return_value=(0, words)):
            out = self.ctrl.find_text("download")
        self.assertTrue(out["found"])
        # centre (300+60, 300+15) = (360, 315)
        self.assertEqual((out["x"], out["y"]), (360, 315))

    def test_window_origin_is_added(self):
        # A window not at 0,0: the box centre is offset by the window origin.
        words = [{"text": "Save", "left": 10, "top": 20, "width": 40,
                  "height": 10, "conf": 90.0}]
        with mock.patch.object(desktop.DesktopController, "_active",
                               return_value={"class": "app"}), \
                mock.patch.object(desktop.DesktopController, "_window_geometry",
                                  return_value=(100, 50, 800, 600)), \
                mock.patch.object(desktop, "run_bin", return_value=(0, b"png")), \
                mock.patch.object(desktop, "_ocr_words", return_value=(0, words)):
            out = self.ctrl.find_text("save")
        # centre (10+20, 20+5) = (30,25) + origin (100,50) = (130,75)
        self.assertEqual((out["x"], out["y"]), (130, 75))

    def test_exact_match_beats_a_confident_longer_one(self):
        words = [
            {"text": "omarchyorg", "left": 10, "top": 10, "width": 50,
             "height": 10, "conf": 99.0},
            {"text": "Omarchy", "left": 200, "top": 200, "width": 40,
             "height": 10, "conf": 60.0},
        ]
        with mock.patch.object(desktop, "hyprctl_json",
                               return_value=[{"x": 0, "y": 0, "width": 100, "height": 100,
                                             "focused": True}]), \
                mock.patch.object(desktop, "run_bin", return_value=(0, b"png")), \
                mock.patch.object(desktop, "_ocr_words", return_value=(0, words)):
            out = self.ctrl.find_text("Omarchy")
        self.assertEqual(out["text"], "Omarchy")

    def test_not_found_is_honest(self):
        with mock.patch.object(desktop, "hyprctl_json",
                               return_value=[{"x": 0, "y": 0, "width": 100, "height": 100,
                                             "focused": True}]), \
                mock.patch.object(desktop, "run_bin", return_value=(0, b"png")), \
                mock.patch.object(desktop, "_ocr_words", return_value=(0, [])):
            out = self.ctrl.find_text("nothing here")
        self.assertFalse(out["found"])
        self.assertNotIn("x", out)

    def test_multi_word_phrase_matches_by_joining_words(self):
        # Regression: OCR returns one entry PER WORD, so "TARGET BETA" (two
        # words) never matched a single-word search and the model got no
        # coordinates.
        words = [
            {"text": "TARGET", "left": 400, "top": 300, "width": 80,
             "height": 20, "conf": 90.0},
            {"text": "BETA", "left": 490, "top": 300, "width": 60,
             "height": 20, "conf": 90.0},
        ]
        with mock.patch.object(desktop, "hyprctl_json",
                               return_value=[{"x": 0, "y": 0, "width": 1000,
                                              "height": 800, "focused": True}]), \
                mock.patch.object(desktop, "run_bin", return_value=(0, b"png")), \
                mock.patch.object(desktop, "_ocr_words", return_value=(0, words)):
            out = self.ctrl.find_text("TARGET BETA")
        self.assertTrue(out["found"])
        # centre of the joined box: x (400+550)/2=475, y (300+320)/2=310
        self.assertEqual((out["x"], out["y"]), (475, 310))

    def test_capture_is_full_resolution(self):
        # Regression: grim -s 0.75 dropped whole words (measured "TARGET BETA"
        # vanished), so a valid target looked absent.
        captured = {}

        def fake_bin(argv, timeout=None):
            captured["argv"] = argv
            return (0, b"png")

        with mock.patch.object(desktop, "hyprctl_json",
                               return_value=[{"x": 0, "y": 0, "width": 800, "height": 600,
                                             "focused": True}]), \
                mock.patch.object(desktop, "run_bin", side_effect=fake_bin), \
                mock.patch.object(desktop, "_ocr_words", return_value=(0, [])):
            self.ctrl.find_text("x")
        self.assertNotIn("-s", captured["argv"])
        self.assertNotIn("0.75", captured["argv"])


class SessionEnvTest(unittest.TestCase):
    """The gateway-spawned server must resolve the graphical-session env lazily.

    Regression: the MCP server is spawned at boot, before Hyprland's Wayland
    socket exists, so it inherits no WAYLAND_DISPLAY. grim/wtype/foot/gtk-launch
    then fail (measured: read_screen "grim capture failed", Chrome launch
    timeout). The fix resolves the vars on each call, not once at startup.
    """

    def test_resolves_wayland_from_runtime_dir(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            Path(d, "wayland-1").write_text("")   # a socket, enough for listing
            Path(d, "wayland-1.lock").write_text("")
            env = {k: v for k, v in desktop.os.environ.items()
                   if k not in ("WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE")}
            with mock.patch.dict(desktop.os.environ, env, clear=True), \
                    mock.patch.dict(desktop.os.environ, {"XDG_RUNTIME_DIR": d}):
                resolved = desktop.session_env()
            self.assertEqual(resolved["WAYLAND_DISPLAY"], "wayland-1")

    def test_does_not_override_an_existing_value(self):
        with mock.patch.dict(desktop.os.environ,
                             {"WAYLAND_DISPLAY": "wayland-9"}, clear=False):
            self.assertEqual(desktop.session_env()["WAYLAND_DISPLAY"], "wayland-9")

    def test_resolves_dbus_session_bus(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            Path(d, "bus").write_text("")
            env = {k: v for k, v in desktop.os.environ.items()
                   if k != "DBUS_SESSION_BUS_ADDRESS"}
            with mock.patch.dict(desktop.os.environ, env, clear=True), \
                    mock.patch.dict(desktop.os.environ, {"XDG_RUNTIME_DIR": d}):
                resolved = desktop.session_env()
            self.assertEqual(resolved["DBUS_SESSION_BUS_ADDRESS"],
                             f"unix:path={d}/bus")

    def test_run_passes_the_resolved_env_by_default(self):
        # A bare run() must carry the session vars, or grim/wtype fail under the
        # gateway. Assert the env it actually hands to subprocess.
        with mock.patch.object(desktop.subprocess, "run") as run_mock:
            run_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            desktop.run(["true"])
        passed = run_mock.call_args.kwargs.get("env")
        self.assertIsNotNone(passed)
        self.assertIn("XDG_RUNTIME_DIR", passed)


if __name__ == "__main__":
    unittest.main()