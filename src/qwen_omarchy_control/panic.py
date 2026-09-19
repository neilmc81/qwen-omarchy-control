"""One panic flag, honoured by every path that delivers input.

Why this exists
---------------
The panic stop (SUPER + SHIFT + ESCAPE) promised to freeze agent input, but the
flag was only read inside `vision.click_element`. The OCR path that delivers the
bulk of multi-step work - `pointer_move`, `mouse_click`, `mouse_scroll`,
`type_text` - never looked at it, so hitting the panic key while the agent worked
through a sequence left the mouse and keyboard fully live. A safety promise that
only half the input paths keep is not a safety promise.

This module is the single source of truth for the flag so the freeze is
*global and mid-sequence*: any tool call that would move the pointer, click,
scroll or type checks it at entry, so a sequence stops at the next step even
though each step is a separate call.

Fail closed
-----------
An unreadable flag path is treated as *not frozen* (so a broken runtime dir does
not wedge the assistant), but the check itself never raises: callers get a clean
`PanicError`.
"""

from __future__ import annotations

import os
from pathlib import Path

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")

# The hotkey script (`bin/qwen-voice-panic.sh`) toggles exactly this file.
PANIC_FILE = Path(os.environ.get(
    "QWEN_VISION_PANIC_FILE", RUNTIME_DIR / "qwen-voice" / "stop"
))


class PanicError(RuntimeError):
    """Input was withheld because the panic freeze is set."""


def panicked() -> bool:
    """True while the user has hit the panic-stop hotkey."""
    try:
        return PANIC_FILE.exists()
    except OSError:
        return False


def set_panic() -> bool:
    """Set the flag. Returns True if it was not already set."""
    if panicked():
        return False
    try:
        PANIC_FILE.parent.mkdir(parents=True, exist_ok=True)
        PANIC_FILE.write_text("")
        return True
    except OSError:
        return False


def clear_panic() -> bool:
    """Remove the flag. Returns True if it had been set."""
    try:
        PANIC_FILE.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def guard(action: str = "input") -> None:
    """Raise PanicError if frozen. Called before every input delivery."""
    if panicked():
        raise PanicError(
            f"agent input is frozen (SUPER + SHIFT + ESCAPE); refused to send "
            f"{action}. Press the key again to clear it."
        )
