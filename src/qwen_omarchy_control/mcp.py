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

from . import (browser, macros, outcome, panic, sequence, task, triage, vision,
               watch)
from .desktop import DesktopController, DesktopError
from .policy import PolicyError, reject_sensitive_text
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
        "name": "find_element",
        "description": "Locate a UI element in a window by what it IS rather than where "
                       "it is, using the window's accessibility tree. Returns the "
                       "element's role, label and a stable token. Read-only (no "
                       "click). Use this whenever a control has a clear name ('the "
                       "Save button', 'the Documents folder') and prefer it to "
                       "read_screen guessing. If it reports the window has no "
                       "accessibility tree, use the OCR tools instead. Level 1.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string",
                         "description": "What you are looking for, e.g. 'the Save "
                                        "button' or 'the Documents folder'."},
                "window": {"type": "string",
                           "description": "Target window: a pid, or a title/app-name "
                                          "substring. Omit to search all windows."},
            },
            "required": ["goal"],
            "additionalProperties": False,
        },
    },
    {
        "name": "click_element",
        "description": "Find a UI element by name (like find_element) and click it via "
                       "the accessibility tree instead of OCR-and-coordinates. This "
                       "MOVES THE REAL MOUSE AND TAKES FOCUS for a moment: a desktop "
                       "notification announces it, so tell the user you are taking "
                       "control and to leave the mouse and keyboard alone until you "
                       "say it is done. It verifies the result and retries once; the "
                       "reply's `verified` field says whether the outcome was "
                       "confirmed - report that honestly to the user rather than "
                       "assuming success. Use for a control with a clear label; for a "
                       "canvas/custom-drawn surface use read_screen + mouse_click. "
                       "Level 2.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string",
                         "description": "What to click, e.g. 'the OK button'."},
                "window": {"type": "string",
                           "description": "Target window: a pid, or a title/app-name "
                                          "substring. Omit for the focused window."},
                "double": {"type": "boolean", "default": False,
                           "description": "Double-click to open an item (folders, "
                                          "list rows). Default single click."},
            },
            "required": ["goal"],
            "additionalProperties": False,
        },
    },
    {
        "name": "describe_actions",
        "description": "Answer \"what can I do here?\" for a window: read its "
                       "accessibility tree and name the 3-5 meaningful actions "
                       "(open, save, send, delete, navigate), best first. "
                       "Read-only: no click, no mouse movement, and allowed even "
                       "while the freeze is set. Use it when the user asks what "
                       "is available in the current window, or to orient before "
                       "clicking. Level 1.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string",
                           "description": "Target window: a pid, or a title/app-name "
                                          "substring. Omit for the focused window."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "do_gui_task",
        "description": "Run a short, bounded, verified GUI task in one window when "
                       "simple primitives are not enough: e.g. 'open the Downloads "
                       "folder', 'fill the name field and submit'. Each step is "
                       "chosen from options built from the window's live state, "
                       "acted on, then re-read to confirm what changed; it stops on "
                       "completion, when stuck, or at an action budget. Literal text "
                       "must be passed in `inputs` and can never be invented. Fails "
                       "soft: returns status 'needs_agent'/'blocked' (never a false "
                       "success) and falls back to browser/OCR tools when the window "
                       "has no accessibility tree (foot, Chrome, Electron). Use for "
                       "a handful of steps, not a long march. Level 2.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string",
                         "description": "What to accomplish in the window, e.g. "
                                        "'open the Documents folder'."},
                "window": {"type": "string",
                           "description": "Target window: a pid, or a title/app-name "
                                          "substring. Omit for the focused window."},
                "inputs": {"type": "object",
                           "description": "Literal values the task may type, keyed by "
                                          "name (e.g. {\"name\": \"Neil\"}). The task "
                                          "may only type these; it cannot invent text.",
                           "additionalProperties": {"type": "string"}},
                "verification": {"type": "array", "items": {"type": "string"},
                                 "description": "Short statements that must be true "
                                                "for the task to count as done."},
                "constraints": {"type": "array", "items": {"type": "string"},
                                "description": "Things the task must not do."},
                "max_actions": {"type": "integer", "minimum": 1, "maximum": 25,
                                "description": "Hard cap on steps (default 8)."},
            },
            "required": ["goal"],
            "additionalProperties": False,
        },
    },
    {
        "name": "browser_read",
        "description": "Read the current browser page as real DOM elements (role, "
                       "name, clickable) instead of OCR. Use this whenever the "
                       "browser is the window in question - it is exact where "
                       "read_screen guesses. Read-only, no mouse movement, and "
                       "allowed while the freeze is set. Optional `goal` narrows "
                       "the result (e.g. 'the search box'). Level 1.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string",
                         "description": "What you are looking for on the page, e.g. "
                                        "'the search box' or 'the Download link'."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "browser_click",
        "description": "Click a link/button on the current browser page by its "
                       "name, via the browser's own DOM (no OCR, no pointer "
                       "movement - it runs in the background, so no takeover "
                       "announcement is needed). Use this instead of "
                       "click_element for anything inside Chrome. It verifies the "
                       "result (URL/title) and reports `verified` honestly. "
                       "Level 2.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string",
                         "description": "What to click, e.g. 'the Download link' "
                                        "or 'Sign in'."},
            },
            "required": ["goal"],
            "additionalProperties": False,
        },
    },
    {
        "name": "browser_type",
        "description": "Type text into a field on the current browser page (found "
                       "by name), via the DOM - no pointer movement. Use for "
                       "search boxes and ordinary forms. NEVER use it for "
                       "passwords, payment or authentication fields. Level 2.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Literal text to type."},
                "goal": {"type": "string",
                         "description": "Which field, e.g. 'the search box'. Omit "
                                        "for the page's only text field."},
                "replace": {"type": "boolean", "default": False,
                            "description": "True to replace the field's current "
                                           "contents instead of appending."},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "describe_outcome",
        "description": "Answer \"did it work?\" about a GUI action: read the window "
                       "and judge whether the goal is now satisfied, returning a "
                       "short sentence in `spoken` to read aloud. Use after a click "
                       "or task when the user asks if it worked. Read-only, never "
                       "clicks. Level 1.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string",
                         "description": "What was meant to happen. Omit to use the "
                                        "most recent recorded action's goal."},
                "window": {"type": "string",
                           "description": "Target window (pid or title substring). "
                                          "Omit for the focused window."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "do_sequence",
        "description": "Run several GUI steps in order in one request ('open "
                       "Documents, make a folder called Taxes, and move the newest "
                       "PDF there'). Each step is a bounded, verified task; the run "
                       "STOPS at the first step it cannot verify and names it, and "
                       "returns a `spoken` summary. Use for a short ordered sequence "
                       "instead of chaining do_gui_task calls yourself. Level 2.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "The overall goal, for the summary."},
                "window": {"type": "string",
                           "description": "Default window for steps (pid/title). Omit for focused."},
                "inputs": {"type": "object",
                           "description": "Literal values steps may type, keyed by name.",
                           "additionalProperties": {"type": "string"}},
                "steps": {"type": "array", "minItems": 1, "maxItems": 12,
                          "items": {
                              "type": "object",
                              "properties": {
                                  "goal": {"type": "string"},
                                  "verification": {"type": "array", "items": {"type": "string"}},
                                  "inputs": {"type": "object",
                                             "additionalProperties": {"type": "string"}},
                                  "window": {"type": "string"},
                              },
                              "required": ["goal"],
                              "additionalProperties": False,
                          }},
            },
            "required": ["goal", "steps"],
            "additionalProperties": False,
        },
    },
    {
        "name": "macro_record",
        "description": "Record a desktop chore once so it can be replayed by name "
                       "later. action='start' with a name begins recording the "
                       "verified steps of subsequent do_gui_task calls; action='stop' "
                       "saves it; action='list' shows saved macros; action='delete' "
                       "removes one. Level 2.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["start", "stop", "list", "delete"]},
                "name": {"type": "string",
                         "description": "Macro name (required for start/delete)."},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "macro_replay",
        "description": "Replay a saved macro by name: re-runs its recorded steps, "
                       "re-targeting live elements and verifying each step, stopping "
                       "honestly at the step that fails. Returns a `spoken` summary. "
                       "Use for 'do my monthly report' after recording it once. "
                       "Level 2.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "watch_start",
        "description": "Watch a window and report when a condition becomes true "
                       "(\"tell me when the export finishes\"). Read-only; never "
                       "clicks. Starts a background job and returns a job_id; poll "
                       "it with watch_check. Level 1.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "condition": {"type": "string",
                              "description": "The condition to watch for, e.g. "
                                             "'the export has finished'."},
                "window": {"type": "string",
                           "description": "Target window (pid/title). Omit for focused."},
                "timeout_s": {"type": "number", "minimum": 5, "maximum": 3600},
                "threshold": {"type": "number", "minimum": 0.5, "maximum": 1.0,
                              "description": "Confidence to count the condition as met (default 0.7)."},
            },
            "required": ["condition"],
            "additionalProperties": False,
        },
    },
    {
        "name": "watch_check",
        "description": "Check a watch job started with watch_start: returns its "
                       "status (running/met/timeout/stopped) and a `spoken` sentence "
                       "when it is done. Level 1.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "watch_stop",
        "description": "Stop a watch job started with watch_start. Level 1.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "browser_navigate",
        "description": "Navigate the browser's tab to an http(s) URL. Prefer "
                       "open_url to open a new page; use this to change the page "
                       "the agent is already working in. Level 2.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "browser_search",
        "description": "Search the web and land on the results page, in one step. "
                       "Prefer this over opening a search engine and driving it "
                       "manually: it navigates, types the query, submits, and "
                       "verifies the results page actually loaded. Use for 'look "
                       "up X', 'search for X', 'google X'. Level 2.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "What to search for."},
            },
            "required": ["query"],
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
        "find_element", "describe_actions", "describe_outcome", "browser_read",
        "watch_start", "watch_check", "watch_stop",
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
        except (DesktopError, PolicyError, panic.PanicError,
                ValueError, vision.VisionError) as exc:
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
        if name == "launch_agent":
            return self._triage_launch_agent(name, args)
        return self._execute(name, args)

    def _triage_launch_agent(self, name: str, args: dict):
        """Pre-dispatch triage for the agent-delegation seam.

        Only a launch that CARRIES A PROMPT submits work to an agent; opening a
        fresh agent window sends nothing and needs no gate. Triage is disabled
        by default and returns `allow` in `off`/`log` modes, so this is a no-op
        until it is deliberately enforced.
        """
        prompt = str(args.get("prompt") or "").strip()
        agent = str(args.get("agent") or "hermes")
        if not prompt:
            return self._execute(name, args)

        verdict = triage.evaluate(prompt, context=f"target agent: {agent}")
        if verdict.verdict == "refuse":
            # Nothing is queued: a mis-heard fragment must not become a task.
            raise PolicyError(
                f"request withheld before reaching {agent}: {verdict.reason}. "
                "Ask the user to repeat or clarify what they want done."
            )
        if verdict.verdict == "confirm":
            desc = f"send {agent} the task: {prompt[:60]}"
            self._pending = (name, args, desc)
            return {
                "pending": desc,
                "triage": {
                    "verdict": verdict.verdict,
                    "route": verdict.route,
                    "confidence": round(verdict.confidence, 3),
                    "destructive": round(verdict.destructive, 3),
                    "reason": verdict.reason,
                },
                "instructions": "This request needs explicit approval before it "
                                "reaches the agent. Ask the user to confirm (e.g. "
                                "\"yes\", \"go ahead\"). If they confirm, call "
                                "confirm_pending. If they decline, call "
                                "cancel_pending.",
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
        if name == "find_element":
            return vision.find_element(
                str(args["goal"]),
                str(args["window"]) if args.get("window") else None,
            )
        if name == "describe_actions":
            return vision.describe_actions(
                str(args["window"]) if args.get("window") else None,
            )
        if name == "do_gui_task":
            inputs = args.get("inputs") or {}
            if not isinstance(inputs, dict):
                raise PolicyError("do_gui_task inputs must be an object")
            for value in inputs.values():
                if reject_sensitive_text(str(value)):
                    raise PolicyError(
                        "refused: an input value looks sensitive "
                        "(password/secret/card). The voice assistant never types "
                        "into sensitive fields."
                    )
            return task.gui_task_safe(
                str(args["goal"]),
                window=str(args["window"]) if args.get("window") else None,
                inputs={str(k): str(v) for k, v in inputs.items()},
                verification=[str(v) for v in (args.get("verification") or [])],
                constraints=[str(v) for v in (args.get("constraints") or [])],
                max_actions=int(args["max_actions"]) if args.get("max_actions") else None,
            )
        if name == "describe_outcome":
            return outcome.describe_outcome(
                str(args["goal"]) if args.get("goal") else None,
                str(args["window"]) if args.get("window") else None,
            )
        if name == "do_sequence":
            inputs = args.get("inputs") or {}
            for value in inputs.values():
                if reject_sensitive_text(str(value)):
                    raise PolicyError(
                        "refused: an input value looks sensitive "
                        "(password/secret/card).")
            return sequence.do_sequence(
                str(args["goal"]),
                args.get("steps") or [],
                window=str(args["window"]) if args.get("window") else None,
                inputs={str(k): str(v) for k, v in inputs.items()},
            )
        if name == "macro_record":
            action = str(args.get("action") or "")
            if action == "start":
                return macros.start(str(args.get("name") or ""))
            if action == "stop":
                return macros.stop()
            if action == "list":
                return {"macros": macros.list_macros()}
            if action == "delete":
                return macros.delete(str(args.get("name") or ""))
            raise PolicyError(f"macro_record action must be start/stop/list/delete")
        if name == "macro_replay":
            return macros.replay(str(args["name"]))
        if name == "watch_start":
            return watch.watch_start(
                str(args["window"]) if args.get("window") else None,
                str(args["condition"]),
                interval_s=float(args.get("interval_s") or 5.0),
                timeout_s=float(args.get("timeout_s") or 900.0),
                threshold=float(args.get("threshold") or 0.7),
            )
        if name == "watch_check":
            return watch.watch_check(str(args["job_id"]))
        if name == "watch_stop":
            return watch.watch_stop(str(args["job_id"]))
        if name == "browser_read":
            return browser.browser_read(
                str(args["goal"]) if args.get("goal") else None,
            )
        if name == "browser_click":
            return browser.browser_click(str(args["goal"]))
        if name == "browser_type":
            text = str(args.get("text") or "")
            if reject_sensitive_text(text):
                raise PolicyError(
                    "refused: text looks sensitive (password/secret/card). The "
                    "voice assistant never types into sensitive fields."
                )
            return browser.browser_type(
                text,
                goal=str(args["goal"]) if args.get("goal") else None,
                replace=bool(args.get("replace", False)),
            )
        if name == "browser_navigate":
            return browser.browser_navigate(str(args["url"]))
        if name == "browser_search":
            query = str(args.get("query") or "")
            if reject_sensitive_text(query):
                raise PolicyError(
                    "refused: the query looks sensitive (password/secret/card)."
                )
            return browser.browser_search(query)
        if name == "click_element":
            return vision.click_element(
                str(args["goal"]),
                str(args["window"]) if args.get("window") else None,
                double=bool(args.get("double", False)),
            )
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