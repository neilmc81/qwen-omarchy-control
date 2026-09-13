"""qwen-omarchy-control - structured local desktop controller for the Qwen voice assistant.

Provides explicit, typed desktop operations (no arbitrary shell execution) that a
voice frontend can call through a stdio MCP server, with a fixed safety policy.
Conservative by design: query operations run immediately, mild state changes run
carefully, and dangerous operations are not exposed here at all (they belong to a
coding backend with its own native permission prompts).

Patterns and command syntax derived from the MIT-licensed omarchy-voice project
(https://github.com/wombatoperator/omarchy-voice) and the Apache-2.0
qwen-audio-agent project (https://github.com/QwenAudio/qwen-audio-agent).
"""

__version__ = "0.1.0"