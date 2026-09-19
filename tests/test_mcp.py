"""Tests for the MCP stdio server wire protocol (no real desktop side effects)."""

import json
import unittest
from pathlib import Path
from unittest import mock

from qwen_omarchy_control import mcp
from qwen_omarchy_control.desktop import DesktopController

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_MCP_CONFIG = REPO_ROOT / "frontend-mcp.json"


def call(handler, method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return json.loads(handler.handle(json.dumps(msg)))


class McpHandshakeTest(unittest.TestCase):
    def test_initialize_echoes_version(self):
        h = mcp.McpHandler()
        res = call(h, "initialize", {"protocolVersion": "2025-06-18",
                                     "capabilities": {}, "clientInfo": {}})
        self.assertEqual(res["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(res["result"]["serverInfo"]["name"], "qwen-omarchy-control")

    def test_tools_list_has_no_shell(self):
        h = mcp.McpHandler()
        res = call(h, "tools/list")
        names = {t["name"] for t in res["result"]["tools"]}
        self.assertIn("switch_workspace", names)
        self.assertIn("type_text", names)  # window-targeted, Level 2
        self.assertNotIn("run_shell", names)
        for tool in res["result"]["tools"]:
            self.assertTrue(tool["inputSchema"]["type"] == "object")

    def test_unknown_tool_errors(self):
        h = mcp.McpHandler()
        res = call(h, "tools/call", {"name": "run_shell", "arguments": {"command": "x"}})
        self.assertTrue(res["result"]["isError"])
        self.assertIn("not exposed", res["result"]["content"][0]["text"])

    def test_describe_actions_is_read_only_and_dispatchable(self):
        h = mcp.McpHandler()
        names = {t["name"] for t in mcp.TOOLS}
        self.assertIn("describe_actions", names)
        self.assertIn("describe_actions", mcp.McpHandler.READ_ONLY)
        # It is dispatched straight through (never gated behind a confirmation).
        with mock.patch("qwen_omarchy_control.vision.describe_actions") as describe:
            describe.return_value = {"window_title": "W", "actions": [], "top_action": None}
            res = call(h, "tools/call",
                       {"name": "describe_actions", "arguments": {"window": "123"}})
        self.assertFalse(res["result"]["isError"])
        describe.assert_called_once_with("123")
        self.assertIsNone(h._pending)

    def test_notifications_ignored(self):
        h = mcp.McpHandler()
        self.assertIsNone(h.handle(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})))

    def test_volume_command_building(self):
        # Verify set_volume/builds the right argv through the mocked executor.
        # set_volume is a gated operation: it returns pending first, then runs
        # on confirm_pending.
        h = mcp.McpHandler()
        with mock.patch("qwen_omarchy_control.desktop.run") as run:
            run.return_value = (0, "")
            res = call(h, "tools/call", {"name": "set_volume", "arguments": {"percent": 42}})
            self.assertFalse(res["result"]["isError"])
            self.assertIn("pending", json.loads(res["result"]["content"][0]["text"]))
            self.assertIsNone(run.call_args)  # not executed yet
            res = call(h, "tools/call", {"name": "confirm_pending", "arguments": {}})
            self.assertFalse(res["result"]["isError"])
            argv = run.call_args.args[0]
            self.assertEqual(argv, ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "42%"])

    def test_workspace_validation(self):
        h = mcp.McpHandler()
        res = call(h, "tools/call", {"name": "switch_workspace", "arguments": {"number": 0}})
        self.assertTrue(res["result"]["isError"])
        res = call(h, "tools/call", {"name": "switch_workspace", "arguments": {"number": 999}})
        self.assertTrue(res["result"]["isError"])


class GatedActionTest(unittest.TestCase):
    def test_close_window_returns_pending_not_executed(self):
        h = mcp.McpHandler()
        with mock.patch("qwen_omarchy_control.desktop.hyprctl_json") as hypr:
            hypr.return_value = {"address": "0x1", "class": "google-chrome",
                                 "title": "YouTube", "initialClass": "google-chrome"}
            res = call(h, "tools/call",
                       {"name": "close_active_window", "arguments": {}})
        self.assertFalse(res["result"]["isError"])
        body = json.loads(res["result"]["content"][0]["text"])
        self.assertIn("pending", body)
        self.assertIn("close google-chrome", body["pending"])
        self.assertIsNotNone(h._pending)

    def test_confirm_pending_executes(self):
        h = mcp.McpHandler()
        window = {"address": "0x1", "class": "foot", "title": "x",
                  "initialClass": "foot"}
        with mock.patch("qwen_omarchy_control.desktop.hyprctl_json") as hypr, \
             mock.patch("qwen_omarchy_control.desktop._dispatch") as dispatch:
            # 1: describe (pending). 2: before. 3+: window is gone.
            hypr.side_effect = [window, window, {}]
            dispatch.return_value = (0, "")
            call(h, "tools/call", {"name": "close_active_window", "arguments": {}})
            self.assertIsNotNone(h._pending)
            res = call(h, "tools/call", {"name": "confirm_pending", "arguments": {}})
        self.assertFalse(res["result"]["isError"])
        body = json.loads(res["result"]["content"][0]["text"])
        self.assertIn("closed active window", body.get("result", ""))
        self.assertIsNone(h._pending)
        self.assertEqual(dispatch.call_count, 1)  # only on confirm

    def test_cancel_pending_discards(self):
        h = mcp.McpHandler()
        with mock.patch("qwen_omarchy_control.desktop.hyprctl_json") as hypr, \
             mock.patch("qwen_omarchy_control.desktop._dispatch") as dispatch:
            hypr.return_value = {"address": "0x1", "class": "foot",
                                 "title": "x", "initialClass": "foot"}
            dispatch.return_value = (0, "")
            call(h, "tools/call", {"name": "close_active_window", "arguments": {}})
            res = call(h, "tools/call", {"name": "cancel_pending", "arguments": {}})
        body = json.loads(res["result"]["content"][0]["text"])
        self.assertIn("cancelled", body["result"])
        self.assertIsNone(h._pending)
        self.assertEqual(dispatch.call_count, 0)  # never executed

    def test_blocked_while_pending(self):
        h = mcp.McpHandler()
        with mock.patch("qwen_omarchy_control.desktop.hyprctl_json") as hypr:
            hypr.return_value = {"address": "0x1", "class": "foot",
                                 "title": "x", "initialClass": "foot"}
            call(h, "tools/call", {"name": "close_active_window", "arguments": {}})
            res = call(h, "tools/call", {"name": "launch_app", "arguments": {"name": "spotify"}})
        self.assertTrue(res["result"]["isError"])
        self.assertIn("confirm", res["result"]["content"][0]["text"])

    def test_read_only_allowed_while_pending(self):
        h = mcp.McpHandler()
        with mock.patch("qwen_omarchy_control.desktop.hyprctl_json") as hypr:
            hypr.return_value = {"address": "0x1", "class": "foot",
                                 "title": "x", "initialClass": "foot"}
            call(h, "tools/call", {"name": "close_active_window", "arguments": {}})
            res = call(h, "tools/call", {"name": "get_active_window", "arguments": {}})
        self.assertFalse(res["result"]["isError"])
        self.assertIn("0x1", json.dumps(res["result"]["content"]))

    def test_confirm_without_pending_errors(self):
        h = mcp.McpHandler()
        res = call(h, "tools/call", {"name": "confirm_pending", "arguments": {}})
        body = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(body["result"], "no pending action")


class MouseToolTest(unittest.TestCase):
    def test_mouse_tools_are_listed(self):
        h = mcp.McpHandler()
        res = call(h, "tools/list")
        names = {t["name"] for t in res["result"]["tools"]}
        for want in ("pointer_move", "mouse_click", "mouse_scroll",
                     "confirm_pending", "cancel_pending"):
            self.assertIn(want, names)

    def test_pointer_move_abs_builds_dispatch(self):
        h = mcp.McpHandler()
        with mock.patch("qwen_omarchy_control.desktop._dispatch") as dispatch:
            dispatch.return_value = (0, "")
            res = call(h, "tools/call",
                       {"name": "pointer_move", "arguments": {"x": 640, "y": 400}})
        self.assertFalse(res["result"]["isError"])
        lua = dispatch.call_args.args[0]
        self.assertIn("cursor.move", lua)
        self.assertIn("x = \"640\"", lua)
        self.assertIn("y = \"400\"", lua)

    def test_pointer_move_relative_reads_cursor(self):
        h = mcp.McpHandler()
        with mock.patch("qwen_omarchy_control.desktop.run") as run, \
             mock.patch("qwen_omarchy_control.desktop.hypr_env") as henv, \
             mock.patch("qwen_omarchy_control.desktop._dispatch") as dispatch:
            henv.return_value = {}
            run.return_value = (0, "100, 50")
            dispatch.return_value = (0, "")
            res = call(h, "tools/call",
                       {"name": "pointer_move", "arguments": {"x": 10, "y": -10, "relative": True}})
        self.assertFalse(res["result"]["isError"])
        lua = dispatch.call_args.args[0]
        self.assertIn("x = \"110\"", lua)  # 100 + 10
        self.assertIn("y = \"40\"", lua)   # 50 - 10

    def test_mouse_click_requires_ydotool(self):
        h = mcp.McpHandler()
        with mock.patch.object(DesktopController, "_ydotool_ready", return_value=False):
            res = call(h, "tools/call", {"name": "mouse_click", "arguments": {}})
        self.assertTrue(res["result"]["isError"])
        self.assertIn("ydotool", res["result"]["content"][0]["text"])

    def test_mouse_click_builds_argv(self):
        h = mcp.McpHandler()
        with mock.patch.object(DesktopController, "_ydotool_ready", return_value=True), \
             mock.patch("qwen_omarchy_control.desktop.run") as run:
            run.return_value = (0, "")
            res = call(h, "tools/call",
                       {"name": "mouse_click", "arguments": {"button": "right", "double": True}})
        self.assertFalse(res["result"]["isError"])
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["ydotool", "click", "--repeat", "2", "0xC1"])

    def test_mouse_scroll_builds_wheel(self):
        h = mcp.McpHandler()
        with mock.patch.object(DesktopController, "_ydotool_ready", return_value=True), \
             mock.patch("qwen_omarchy_control.desktop.hyprctl_json") as hypr, \
             mock.patch("qwen_omarchy_control.desktop._dispatch") as dispatch, \
             mock.patch("qwen_omarchy_control.desktop.run") as run:
            hypr.return_value = {"address": "0x1", "class": "google-chrome",
                                 "title": "x", "initialClass": "google-chrome",
                                 "at": [0, 0], "size": [1000, 800]}
            dispatch.return_value = (0, "")
            run.return_value = (0, "")
            res = call(h, "tools/call",
                       {"name": "mouse_scroll", "arguments": {"direction": "down"}})
        self.assertFalse(res["result"]["isError"])
        # Points the wheel at the window center first.
        self.assertTrue(any("cursor.move" in a.args[0] for a in dispatch.call_args_list))
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["ydotool", "mousemove"])
        self.assertEqual(argv[2], "--wheel")
        self.assertEqual(argv[3], "-y")
        self.assertTrue(int(argv[4]) < 0)  # "down" is a negative wheel delta


class FrontendAllowlistTest(unittest.TestCase):
    """The frontend only forwards tools enabled in frontend-mcp.json.

    Two failure modes this pins down, both silent to a casual reader:
      * a tool enabled in the allowlist but missing from the server makes the
        frontend client throw and drop the whole MCP connection;
      * a tool on the server but absent from the allowlist is invisible to the
        voice model - which is exactly how describe_actions was first shipped
        and why asking "what can I do here?" got a generic answer.
    """

    def setUp(self):
        self.config = json.loads(FRONTEND_MCP_CONFIG.read_text())
        server = self.config["servers"]["qwen_omarchy_control"]
        self.enabled = {name for name, policy in server["tools"].items()
                        if policy.get("enabled")}
        self.served = {tool["name"] for tool in mcp.TOOLS}

    def test_every_enabled_tool_exists_on_the_server(self):
        missing = self.enabled - self.served
        self.assertEqual(
            missing, set(),
            f"frontend-mcp.json enables tools the MCP server does not expose; "
            f"the frontend client would throw and drop the connection: "
            f"{sorted(missing)}")

    def test_every_conversational_tool_is_enabled(self):
        # A tool the model is told to call must actually be reachable.
        for name in ("find_element", "click_element", "describe_actions"):
            self.assertIn(
                name, self.enabled,
                f"{name} is served but not enabled in frontend-mcp.json, so the "
                "voice model never sees it")

    def test_describe_actions_is_enabled_and_described(self):
        server = self.config["servers"]["qwen_omarchy_control"]
        policy = server["tools"]["describe_actions"]
        self.assertTrue(policy["enabled"])
        self.assertIn("what can i do here", policy["description"].lower())


if __name__ == "__main__":
    unittest.main()