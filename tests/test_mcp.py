"""Tests for the MCP stdio server wire protocol (no real desktop side effects)."""

import json
import unittest
from unittest import mock

from qwen_omarchy_control import mcp


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
        self.assertNotIn("run_shell", names)
        self.assertNotIn("type_text", names)  # deliberately not exposed over voice
        for tool in res["result"]["tools"]:
            self.assertTrue(tool["inputSchema"]["type"] == "object")

    def test_unknown_tool_errors(self):
        h = mcp.McpHandler()
        res = call(h, "tools/call", {"name": "run_shell", "arguments": {"command": "x"}})
        self.assertTrue(res["result"]["isError"])
        self.assertIn("not exposed", res["result"]["content"][0]["text"])

    def test_notifications_ignored(self):
        h = mcp.McpHandler()
        self.assertIsNone(h.handle(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})))

    def test_volume_command_building(self):
        # Verify set_volume/builds the right argv through the mocked executor.
        h = mcp.McpHandler()
        with mock.patch("qwen_omarchy_control.desktop.run") as run:
            run.return_value = (0, "")
            res = call(h, "tools/call", {"name": "set_volume", "arguments": {"percent": 42}})
            self.assertFalse(res["result"]["isError"])
            argv = run.call_args.args[0]
            self.assertEqual(argv, ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "42%"])

    def test_workspace_validation(self):
        h = mcp.McpHandler()
        res = call(h, "tools/call", {"name": "switch_workspace", "arguments": {"number": 0}})
        self.assertTrue(res["result"]["isError"])
        res = call(h, "tools/call", {"name": "switch_workspace", "arguments": {"number": 999}})
        self.assertTrue(res["result"]["isError"])


if __name__ == "__main__":
    unittest.main()