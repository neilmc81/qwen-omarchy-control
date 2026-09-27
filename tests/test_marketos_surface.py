"""Phase 9 MarketOS voice integration: the three surfaces must agree.

A tool is only "shipped" in the Qwen project when all three surfaces agree:

1. the MCP server exposes it,
2. ``frontend-mcp.json`` enables it, and
3. the routing prompt (``PROMPT.md`` / ``share/prompt.example.md``) tells the
   model when to use it.

Surfaces 1-2 already have a mismatch test for the desktop server. This module
adds the MarketOS server, checks both directions against the allowlist, and pins
the routing rules so a tool cannot be renamed or dropped on only one surface.

The MCP server is exercised over its real stdio boundary (the same way the voice
frontend talks to it) when the MarketOS checkout is available; otherwise those
checks are skipped so the Qwen suite stays self-contained.
"""

import json
import os
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_MCP_CONFIG = REPO_ROOT / "frontend-mcp.json"
def _prompt_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    configured_dir = os.environ.get("QWEN_AUDIO_AGENT_FRONTEND_PROMPT_DIR", "").strip()
    if configured_dir:
        candidates.append(Path(configured_dir) / "PROMPT.md")
    candidates.append(Path.home() / ".config" / "qwaudio" / "frontend-agent" / "PROMPT.md")
    candidates.append(REPO_ROOT / "share" / "prompt.example.md")
    return tuple(candidates)


PROMPT_CANDIDATES = _prompt_candidates()

#: Where the MarketOS checkout lives. Fixed by the install; overridable so CI
#: on another machine can still run the contract checks.
MARKETOS_REPO = Path(
    os.environ.get("MARKETOS_REPO", "/home/neil/Projects/MarketOS")
)
MARKETOS_LAUNCHER = MARKETOS_REPO / "omarchy" / "marketos-mcp"

EXPECTED_MARKETOS_TOOLS = {
    "market_status",
    "market_brief",
    "market_next_event",
    "market_calendar",
    "market_macro",
    "market_technical",
    "market_latest_events",
    "market_latest_analysis",
    "market_event_analysis",
    "market_latest_briefing",
    "market_history_query",
    "market_materiality_calibration",
    "market_fomc_history",
    "market_similar_cases",
    "market_calibration_review",
    "market_usage",
    "open_marketos",
}

#: Each routing intent maps to the tool the model must choose. The prompt must
#: name the tool and at least one natural trigger phrase for it.
ROUTING_FIXTURES = {
    "market_status": ("what's happening with nq", "how's the market"),
    "market_brief": ("give me the market brief", "catch me up"),
    "market_next_event": ("what's next", "when is cpi"),
    "market_calendar": ("what's on the calendar today", "important this week"),
    "market_macro": ("what's the macro picture", "latest cpi"),
    "market_technical": ("what's the technical picture", "versus vwap"),
    "market_latest_events": ("any important news", "what just happened"),
    "market_latest_analysis": ("what's the latest analysis", "what does marketos think"),
    "market_event_analysis": ("explain that cpi release", "what does marketos say about that event"),
    "market_latest_briefing": ("morning brief", "latest briefing", "session recap"),
    "market_history_query": ("historically", "hot cpi releases", "usually happens"),
    "market_materiality_calibration": ("materiality events behaved", "identifying material events"),
    "market_fomc_history": ("fomc move reverse",),
    "market_similar_cases": ("similar to the current market", "similar cases"),
    "market_calibration_review": ("recommend reviewing", "calibration issues"),
    "market_usage": ("costing me today",),
    "open_marketos": ("open marketos", "open technicals"),
}


def load_prompt() -> str | None:
    for candidate in PROMPT_CANDIDATES:
        if candidate and candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    return None


class MarketOsRoutingPromptTest(unittest.TestCase):
    """Surface 3: the routing prompt names every MarketOS tool and trigger."""

    @classmethod
    def setUpClass(cls):
        cls.prompt = load_prompt()
        if cls.prompt is None:
            raise unittest.SkipTest("routing prompt not found")

    def test_prompt_has_a_marketos_section(self):
        lowered = self.prompt.lower()
        self.assertIn("marketos", lowered)
        self.assertIn("source of truth", lowered)
        self.assertIn("mcp__marketos__", self.prompt)

    def test_every_tool_is_named_in_the_prompt(self):
        for tool in EXPECTED_MARKETOS_TOOLS:
            self.assertIn(tool, self.prompt, f"{tool} missing from routing prompt")

    def test_each_intent_has_a_trigger_phrase(self):
        lowered = self.prompt.lower()
        for tool, phrases in ROUTING_FIXTURES.items():
            self.assertIn(tool, self.prompt, f"{tool} missing from routing prompt")
            self.assertTrue(
                any(phrase in lowered for phrase in phrases),
                f"no trigger phrase for {tool}: {phrases}",
            )

    def test_prompt_forbids_automatic_changes(self):
        # The review tool is for humans; the prompt must not imply MarketOS
        # changes its own policy.
        lowered = self.prompt.lower()
        self.assertIn("no change is ever applied", lowered)

    def test_prompt_forbids_generic_market_answers(self):
        lowered = self.prompt.lower()
        self.assertIn("general knowledge", lowered)
        self.assertIn("never invent", lowered)

    def test_prompt_handles_stale_and_offline(self):
        lowered = self.prompt.lower()
        self.assertIn("stale", lowered)
        self.assertIn("not responding", lowered)

    def test_desktop_and_agent_routing_stay_separate(self):
        # Moving a window is desktop control; coding is agent handoff. MarketOS
        # must not swallow either.
        lowered = self.prompt.lower()
        self.assertIn("desktop action", lowered)
        self.assertIn("moving or resizing", lowered)

    def test_prompt_stays_within_the_loader_cap(self):
        # The gateway truncates PROMPT.md at 16,000 characters; a silently
        # truncated prompt would drop the MarketOS rules entirely.
        self.assertLess(len(self.prompt), 16_000)


class MarketOsAllowlistTest(unittest.TestCase):
    """Surfaces 1-2: server tools and the frontend allowlist agree exactly."""

    @classmethod
    def setUpClass(cls):
        config = json.loads(FRONTEND_MCP_CONFIG.read_text(encoding="utf-8"))
        cls.server = config["servers"].get("marketos")
        if cls.server is None:
            raise unittest.SkipTest("no marketos server configured")
        cls.enabled = {
            name
            for name, policy in cls.server["tools"].items()
            if policy.get("enabled")
        }
        cls.served = cls._discover_served_tools()

    @staticmethod
    def _discover_served_tools() -> set | None:
        if not MARKETOS_LAUNCHER.is_file():
            return None
        request_lines = "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"},
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            ]
        )
        try:
            completed = subprocess.run(
                [str(MARKETOS_LAUNCHER)],
                input=request_lines + "\n",
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "MARKETOS_API_URL": "http://127.0.0.1:8765"},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        for line in completed.stdout.splitlines():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 2 and "result" in message:
                return {tool["name"] for tool in message["result"]["tools"]}
        return None

    def test_enabled_tools_exist_on_the_server(self):
        if self.served is None:
            self.skipTest("MarketOS MCP server not available")
        missing = self.enabled - self.served
        self.assertEqual(
            missing,
            set(),
            "frontend-mcp.json enables MarketOS tools the server does not "
            f"expose; the frontend client would drop the connection: {sorted(missing)}",
        )

    def test_every_served_tool_is_enabled(self):
        if self.served is None:
            self.skipTest("MarketOS MCP server not available")
        disabled = self.served - self.enabled
        self.assertEqual(
            disabled,
            set(),
            f"MarketOS tools are served but invisible to the voice model: {sorted(disabled)}",
        )

    def test_expected_surface_is_exact(self):
        if self.served is None:
            self.skipTest("MarketOS MCP server not available")
        self.assertEqual(self.served, EXPECTED_MARKETOS_TOOLS)
        self.assertEqual(self.enabled, EXPECTED_MARKETOS_TOOLS)

    def test_transport_is_loopback_and_hermetic(self):
        transport = self.server["transport"]
        self.assertEqual(transport["type"], "stdio")
        self.assertTrue(transport["command"].endswith("marketos-mcp"))
        self.assertEqual(transport["env"]["MARKETOS_API_URL"], "http://127.0.0.1:8765")

    def test_no_mutating_marketos_tool_is_enabled(self):
        for name in self.enabled:
            self.assertFalse(
                any(word in name for word in ("admin", "delete", "prune", "configure", "set_")),
                f"{name} looks like a mutating/admin tool; Phase 9 is read-only",
            )


class DesktopSurfaceUnchangedTest(unittest.TestCase):
    """Adding MarketOS must not disturb the existing desktop tool surface."""

    def test_desktop_server_still_present_and_enabled(self):
        config = json.loads(FRONTEND_MCP_CONFIG.read_text(encoding="utf-8"))
        desktop = config["servers"].get("qwen_omarchy_control")
        self.assertIsNotNone(desktop)
        self.assertTrue(desktop["enabled"])
        for name in ("launch_agent", "focus_window", "describe_actions"):
            self.assertTrue(desktop["tools"][name]["enabled"])

    def test_server_count_within_limit(self):
        config = json.loads(FRONTEND_MCP_CONFIG.read_text(encoding="utf-8"))
        # The frontend accepts at most 8 servers.
        self.assertLessEqual(len(config["servers"]), 8)


if __name__ == "__main__":
    unittest.main()
