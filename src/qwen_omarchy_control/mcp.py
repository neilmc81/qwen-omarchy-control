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
                       "'browser', 'files', 'editor', and the agent names 'hermes', 'codex', "
                       "'opencode' (opens that agent's TUI in a terminal window). "
                       "Examples: 'chrome', 'spotify', 'terminal'. Level 1.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "launch_agent",
        "description": "Open a VISIBLE agent TUI window (hermes, codex, or opencode) and let the "
                       "user watch it work. Use WITH a prompt to run a coding/build request "
                       "(prompt = the user's request). Use WITHOUT a prompt when the user just "
                       "wants to OPEN the agent (e.g. 'open hermes', 'open the agent', "
                       "'open default agent', 'open hermes tui') - that opens a fresh session "
                       "and must NOT send any text to the agent. Level 2 (1 with no prompt).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string",
                          "description": "'hermes' (default), 'codex' or 'opencode'."},
                "prompt": {"type": "string",
                           "description": "The user's request as a clear instruction. OMIT when "
                                          "just opening the agent."},
            },
            "required": ["agent"],
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
        "name": "read_window",
        "description": "OCR a window and return its on-screen text. Use this to read back what an "
                       "application or agent produced (e.g. the coding agent's answer in its "
                       "terminal) so you can summarize it for the user. Windows: 'opencode', "
                       "'hermes', 'terminal', 'browser', or a class/title substring. Level 1.",
        "inputSchema": {
            "type": "object",
            "properties": {"window": {"type": "string",
                                      "description": "Window to read (alias or substring); "
                                                     "empty = the focused window."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "read_screen",
        "description": "OCR the whole focused monitor and return the text. Level 1.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "pointer_move",
        "description": "Move the mouse pointer to an absolute screen position (x, y), or by a "
                       "delta when relative=true. Use together with read_window/read_screen "
                       "and mouse_click to operate any GUI app. Level 1.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Absolute x, or delta x when relative."},
                "y": {"type": "integer", "description": "Absolute y, or delta y when relative."},
                "relative": {"type": "boolean", "default": False,
                             "description": "True to move by (x, y) from the current pointer."},
            },
            "required": ["x", "y"],
            "additionalProperties": False,
        },
    },
    {
        "name": "mouse_click",
        "description": "Click the mouse button at the current pointer position. Move the "
                       "pointer first (pointer_move) unless you are clicking where it already "
                       "is. Level 2.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "button": {"type": "string", "enum": ["left", "right", "middle"],
                           "description": "Which button; defaults to left."},
                "double": {"type": "boolean", "default": False,
                           "description": "True to double-click (e.g. open an item)."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "mouse_scroll",
        "description": "Scroll a window (or wherever the pointer is) by about one screenful "
                       "per page. The pointer is moved to the window's center first. "
                       "Read the screen again afterwards; the content changed. Level 2.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "pages": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1,
                          "description": "Screenfuls to scroll (default 1)."},
                "window": {"type": "string",
                           "description": "Window to scroll (alias, class or title); "
                                          "empty = the focused window."},
            },
            "required": ["direction"],
            "additionalProperties": False,
        },
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
    {
        "name": "confirm_pending",
        "description": "Run the action that is waiting for confirmation (returned by a "
                       "gated tool as {\"pending\": ...}). Call this after the user "
                       "confirms. Level 2.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "cancel_pending",
        "description": "Discard the action that is waiting for confirmation. Call this "
                       "after the user declines. Level 1.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


class McpHandler:
    # Read-only tools stay usable while an action is waiting for confirmation.
    READ_ONLY = frozenset({
        "get_active_window", "list_windows", "list_workspaces", "get_monitors",
        "get_audio_status", "get_system_status", "read_window", "read_screen",
    })

    def __init__(self) -> None:
        self.controller = DesktopController()
        self._pending: tuple[str, dict, str] | None = None
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
        if name == "confirm_pending":
            return self._resolve_pending(confirm=True)
        if name == "cancel_pending":
            return self._resolve_pending(confirm=False)
        if self._pending is not None and name not in self.READ_ONLY:
            _, _, desc = self._pending
            raise PolicyError(
                f"an action is waiting for confirmation: {desc}. "
                "Call confirm_pending to run it, or cancel_pending to discard "
                "it, before doing anything else.")
        if name in DesktopController.GATED:
            desc = self.controller.describe(name, **args)
            self._pending = (name, args, desc)
            return {
                "pending": desc,
                "instructions": "Ask the user to confirm (e.g. \"yes\", \"go "
                                "ahead\"). If they confirm, call confirm_pending. "
                                "If they decline, call cancel_pending.",
            }
        return self._execute(name, args)

    def _resolve_pending(self, confirm: bool) -> dict:
        if self._pending is None:
            return {"result": "no pending action"}
        name, args, desc = self._pending
        self._pending = None
        if not confirm:
            return {"result": f"cancelled: {desc}"}
        return self._execute(name, args)

    def _execute(self, name: str, args: dict):
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
        if name == "launch_agent":
            return {"result": ctrl.launch_agent(
                str(args.get("agent") or "hermes"), str(args.get("prompt") or ""))}
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
        if name == "read_window":
            return {"result": ctrl.read_window(str(args["window"]) if args.get("window") else None)}
        if name == "read_screen":
            return {"result": ctrl.read_screen()}
        if name == "pointer_move":
            return {"result": ctrl.pointer_move(
                int(args["x"]), int(args["y"]), bool(args.get("relative", False)))}
        if name == "mouse_click":
            return {"result": ctrl.mouse_click(
                str(args.get("button") or "left"), bool(args.get("double", False)))}
        if name == "mouse_scroll":
            return {"result": ctrl.mouse_scroll(
                str(args["direction"]), int(args.get("pages", 1)),
                str(args["window"]) if args.get("window") else None)}
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