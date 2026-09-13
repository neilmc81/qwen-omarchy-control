"""Minimal MCP (Model Context Protocol) stdio server exposing the desktop controller.

Speaks newline-delimited JSON-RPC 2.0 over stdin/stdout -- the same protocol the
official @modelcontextprotocol/sdk uses for stdio transports, which is what the
qwen-audio-agent frontend MCP client connects to. No third-party dependencies.

Exposes only the allowlisted desktop operations; a generic shell tool is
intentionally absent.
"""

from __future__ import annotations

import json
import sys
import traceback
import uuid

from .desktop import DesktopController, DesktopError
from .policy import PolicyError

PROTOCOL_VERSION = "2025-06-18"

SERVER_INFO = {"name": "qwen-omarchy-control", "version": "0.1.0"}

# Tool schema for the MCP layer. Arguments map 1:1 to controller methods.
# Level 1 (immediate) and Level 2 (careful) operations only.
TOOLS = [
    {
        "name": "get_active_window",
        "description": "Return the focused window (class, title, workspace, address). Level 1.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_windows",
        "description": "List all open windows with class, title, workspace and address. Level 1.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_workspaces",
        "description": "List workspaces with id, name, monitor and window count. Level 1.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_monitors",
        "description": "List connected monitors. Level 1.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "switch_workspace",
        "description": "Switch to workspace <number> (1-100). Level 1.",
        "inputSchema": {
            "type": "object",
            "properties": {"number": {"type": "integer", "minimum": 1, "maximum": 100}},
            "required": ["number"],
            "additionalProperties": False,
        },
    },
    {
        "name": "focus_window",
        "description": "Focus the window whose class or title matches <app_or_title>, "
                       "e.g. 'ghostty' or 'my project'. Level 1.",
        "inputSchema": {
            "type": "object",
            "properties": {"app_or_title": {"type": "string"}},
            "required": ["app_or_title"],
            "additionalProperties": False,
        },
    },
    {
        "name": "launch_app",
        "description": "Launch an installed application by name. Also understands 'terminal', "
                       "'browser', 'files', 'editor'. Examples: 'chrome', 'spotify', 'terminal'. "
                       "Level 1.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_volume",
        "description": "Set the speaker volume to <percent> (0-100). Level 1.",
        "inputSchema": {
            "type": "object",
            "properties": {"percent": {"type": "integer", "minimum": 0, "maximum": 100}},
            "required": ["percent"],
            "additionalProperties": False,
        },
    },
    {
        "name": "volume_up",
        "description": "Raise the speaker volume a step (shows the Omarchy OSD). Level 1.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "volume_down",
        "description": "Lower the speaker volume a step (shows the Omarchy OSD). Level 1.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "mute_audio",
        "description": "Mute the computer's speaker output. Level 1.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "unmute_audio",
        "description": "Unmute the computer's speaker output. Level 1.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_audio_status",
        "description": "Current speaker volume, mute state, and input device state. Level 1.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_system_status",
        "description": "OS, kernel, hostname, uptime, memory and time. Level 1.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "move_active_window_to_workspace",
        "description": "Move the focused window to workspace <number> (keep focus unless "
                       "<follow> is true). Level 2.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "number": {"type": "integer", "minimum": 1, "maximum": 100},
                "follow": {"type": "boolean", "default": False},
            },
            "required": ["number"],
            "additionalProperties": False,
        },
    },
    {
        "name": "close_active_window",
        "description": "Close the focused application/window. Level 2.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "open_url",
        "description": "Open an http(s) URL in the default browser. Level 2.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "type_text",
        "description": "Focus a window and type text into it, optionally pressing Enter to "
                       "send. This is how you hand a request to the user's coding agent "
                       "(e.g. window='opencode' or 'coding agent', or a terminal): the user "
                       "asks, you type the prompt and send=True. Only type user-approved, "
                       "non-sensitive content; never type into password/payment/auth fields. "
                       "Level 2.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string",
                           "description": "Window to type into: 'opencode'/'coding agent', "
                                          "'terminal', 'browser', or a class/title substring. "
                                          "Empty = the focused window."},
                "text": {"type": "string", "description": "Literal text to type."},
                "send": {"type": "boolean", "default": False,
                         "description": "Press Enter afterwards (submit the prompt)."},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
]


class McpHandler:
    def __init__(self) -> None:
        self.controller = DesktopController()
        self._pending_notes: list[dict] = []
        self._request_id = 0

    # -- JSON-RPC ---------------------------------------------------------
    def handle(self, line: str) -> str | None:
        """Process one incoming JSON line; return a response line or None."""
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return self._error(None, "Parse error", -32700)
        method = msg.get("method")
        msg_id = msg.get("id")
        if method is None and "id" in msg:
            # A response from a request we (server) never made; ignore.
            return None

        if method == "initialize" and "id" in msg:
            params = msg.get("params", {})
            requested = (params or {}).get("protocolVersion") or PROTOCOL_VERSION
            # Echo the client's requested version so version negotiation always
            # succeeds; the core subset we implement is stable across versions.
            return self._result(msg_id, {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            })
        if method == "notifications/initialized":
            return None
        if method == "notifications/cancelled":
            return None
        if method == "ping" and "id" in msg:
            return self._result(msg_id, {})
        if method == "tools/list" and "id" in msg:
            return self._result(msg_id, {"tools": TOOLS})
        if method == "tools/call" and "id" in msg:
            return self._call_tool(msg_id, msg.get("params", {}))
        if method == "resources/list" and "id" in msg:
            return self._result(msg_id, {"resources": []})
        if "id" in msg:
            return self._error(msg_id, f"method not supported: {method}", -32601)
        return None

    def _call_tool(self, msg_id, params: dict) -> str:
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            result = self._run_tool(name, args)
            return self._result(msg_id, {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "isError": False,
            })
        except (DesktopError, PolicyError, ValueError) as exc:
            return self._result(msg_id, {
                "content": [{"type": "text", "text": f"ERROR: {exc}"}],
                "isError": True,
            })
        except Exception as exc:  # noqa: BLE001 - surface to log, keep server alive
            traceback.print_exc()
            return self._result(msg_id, {
                "content": [{"type": "text", "text": f"INTERNAL ERROR: {exc}"}],
                "isError": True,
            })

    def _run_tool(self, name: str, args: dict):
        ctrl = self.controller
        if name == "get_active_window":
            return ctrl.get_active_window()
        if name == "list_windows":
            return {"windows": ctrl.list_windows()}
        if name == "list_workspaces":
            return {"workspaces": ctrl.list_workspaces()}
        if name == "get_monitors":
            return {"monitors": ctrl.get_monitors()}
        if name == "switch_workspace":
            return {"result": ctrl.switch_workspace(int(args["number"]))}
        if name == "focus_window":
            return ctrl.focus_window(str(args["app_or_title"]))
        if name == "launch_app":
            return {"result": ctrl.launch_app(str(args["name"]))}
        if name == "set_volume":
            return {"result": ctrl.set_volume(int(args["percent"]))}
        if name == "volume_up":
            return {"result": ctrl.volume_up()}
        if name == "volume_down":
            return {"result": ctrl.volume_down()}
        if name == "mute_audio":
            return {"result": ctrl.mute_audio()}
        if name == "unmute_audio":
            return {"result": ctrl.unmute_audio()}
        if name == "get_audio_status":
            return ctrl.get_audio_status()
        if name == "get_system_status":
            return ctrl.get_system_status()
        if name == "move_active_window_to_workspace":
            return {"result": ctrl.move_active_window_to_workspace(
                int(args["number"]), bool(args.get("follow", False)))}
        if name == "close_active_window":
            return {"result": ctrl.close_active_window()}
        if name == "open_url":
            return {"result": ctrl.open_url(str(args["url"]))}
        if name == "type_text":
            return {"result": ctrl.type_text(
                str(args["window"]) if args.get("window") else None,
                str(args["text"]),
                bool(args.get("send", False)),
            )}
        raise PolicyError(f"tool not exposed by this server: {name!r}")

    # -- encoding helpers ---------------------------------------------------
    def _result(self, msg_id, result: dict) -> str:
        return json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result},
                          ensure_ascii=False)

    def _error(self, msg_id, message: str, code: int) -> str:
        return json.dumps({"jsonrpc": "2.0", "id": msg_id,
                           "error": {"code": code, "message": message}},
                          ensure_ascii=False)


def serve() -> int:
    handler = McpHandler()
    for line in sys.stdin:
        if not line or not line.strip():
            continue
        response = handler.handle(line.rstrip("\n"))
        if response:
            sys.stdout.write(response + "\n")
            sys.stdout.flush()
    return 0