"""Safety policy for desktop operations.

Levels
------
LEVEL 1 - run immediately: read-only queries, workspace switching, focusing,
launching ordinary apps, volume changes, muting, system status.

LEVEL 2 - run carefully: moving a window, closing an ordinary window, opening a
URL, typing text into an ordinary (non-sensitive) app.

LEVEL 3 - never exposed to the voice frontend here. Destructive or sensitive
actions (file deletion, package removal, sudo, shutdown, killing processes,
sending messages, financial actions, typing into password fields, arbitrary
shell) are NOT implemented as voice tools; they are left to the coding backend,
which keeps its own per-action permission prompts.

Rationale: an open microphone is an untrusted input channel. A misheard or
malicious sentence must not be able to reformat a disk or submit a form.
"""

from __future__ import annotations

import re
from typing import Iterable

LEVELS = {"level1", "level2", "level3"}


class PolicyError(Exception):
    """The operation is unknown or outside the permitted voice surface."""


class Denied(PolicyError):
    """The operation was denied outright."""


BLOCKED_WORDS = re.compile(
    r"\b(password|passwd|pwd|payment|credit\s*card|ssn|social\s*security|"
    r"bank|wire\s*transfer|sudo|rm\s+-rf|mkfs|dd\s+of=)\b",
    re.IGNORECASE,
)


def classify(operation: str) -> str:
    """Which level an operation belongs to.

    Unknown operations fail closed (PolicyError); there is no general "run
    anything" escape hatch.
    """
    if operation in {
        "get_active_window",
        "list_windows",
        "list_workspaces",
        "get_monitors",
        "switch_workspace",
        "focus_window",
        "launch_app",
        "set_volume",
        "volume_up",
        "volume_down",
        "mute_audio",
        "unmute_audio",
        "get_audio_status",
        "get_system_status",
        "read_window",
        "read_screen",
    }:
        return "level1"
    if operation in {
        "move_active_window_to_workspace",
        "close_active_window",
        "open_url",
        "type_text",
    }:
        return "level2"
    if operation in {"delete_file", "overwrite_file", "run_shell",
                     "shutdown", "reboot", "kill_process", "send_message",
                     "submit_form", "install_package", "remove_package"}:
        raise Denied(
            f"{operation} is a level-3 operation and is not exposed to the "
            "voice frontend. Route it through the coding backend instead."
        )
    raise PolicyError(f"unknown desktop operation: {operation!r}")


def classify_many(operations: Iterable[str]) -> dict[str, str]:
    return {op: classify(op) for op in operations}


def max_level(operations: Iterable[str]) -> str:
    """Highest level among a set of operations (for request-level gating).

    Fails closed: an unknown operation raises PolicyError rather than being
    silently downgraded.
    """
    levels = {"level1": 1, "level2": 2, "level3": 3}
    highest = "level1"
    for op in operations:
        lvl = classify(op)
        if levels[lvl] > levels[highest]:
            highest = lvl
    return highest


def reject_sensitive_text(text: str) -> bool:
    """Heuristic guard: refuse to type obviously sensitive content.

    This is a best-effort local guard, not a sandbox. The primary protection is
    that the voice model is instructed to never type into sensitive fields and
    that no L3 automation exists on the voice path.
    """
    return bool(BLOCKED_WORDS.search(text or ""))