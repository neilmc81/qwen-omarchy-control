#!/usr/bin/env python3
"""Apply the qwen-audio-agent TUI patch that makes the TUI own the bar state.

The Omarchy bar indicator reads $XDG_RUNTIME_DIR/qwen-voice/state.json, and the
toggle hotkey only sends '/m' to the hidden TUI. Before this patch the toggle
script tracked its own copy of the mute state, so any '/m' sent outside the
script (or the TUI muting itself when voice ownership moves) left the icon
showing "muted" while the microphone was actually listening.

This patch makes the TUI the single source of truth for that file:
  * the TUI reads the file at startup and starts muted unless it says listening
  * it writes its real state on every mute/unmute, on self-mute events, and on
    exit (stopped)
  * it re-asserts the mute to the gateway when voice ownership becomes active

Run after reinstalling/upgrading qwen-audio-agent. Safe to re-run (idempotent).
"""

from __future__ import annotations

import sys
from pathlib import Path


def find_tui() -> Path:
    candidates = [
        Path(sys.argv[1]) if len(sys.argv) > 1 else None,
    ]
    candidates = [c for c in candidates if c]
    import subprocess
    try:
        prefix = subprocess.run(
            ["npm", "prefix", "-g"], capture_output=True, text=True,
            check=True,
        ).stdout.strip()
        candidates.append(Path(prefix) / "lib/node_modules/qwen-audio-agent/tui/src/index.mjs")
    except Exception:
        pass
    for c in candidates:
        if c and c.exists():
            return c
    raise SystemExit("could not locate tui/src/index.mjs (pass the path as argv[1])")


REPLACEMENTS: list[tuple[str, str, int]] = [
    # 1. fs imports for the state file helpers.
    (
        "import { pathToFileURL } from 'node:url'\n",
        "import { pathToFileURL } from 'node:url'\n"
        "import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'\n",
        1,
    ),
    # 2. State-file helpers, right after the style() helper.
    (
        "function style(text, color) {\n"
        "  if (!process.stdout.isTTY || process.env.NO_COLOR) return text\n"
        "  return `${ANSI[color]}${text}${ANSI.reset}`\n"
        "}\n",
        "function style(text, color) {\n"
        "  if (!process.stdout.isTTY || process.env.NO_COLOR) return text\n"
        "  return `${ANSI[color]}${text}${ANSI.reset}`\n"
        "}\n"
        "\n"
        "// --- voice-state file (the bar indicator's single source of truth) ---\n"
        "// The TUI owns $XDG_RUNTIME_DIR/qwen-voice/state.json: it is written on\n"
        "// every transition (and at start/stop) so the Omarchy bar indicator always\n"
        "// matches whether the microphone is actually listening.\n"
        "const VOICE_STATE_FILE = process.env.QWEN_AUDIO_TUI_STATE_FILE || (\n"
        "  process.env.XDG_RUNTIME_DIR || `/run/user/${process.getuid()}`\n"
        ") + '/qwen-voice/state.json'\n"
        "\n"
        "function voiceStateDirname(path) {\n"
        "  const i = path.lastIndexOf('/')\n"
        "  return i > 0 ? path.slice(0, i) : '.'\n"
        "}\n"
        "\n"
        "function writeVoiceState(status, label) {\n"
        "  try {\n"
        "    mkdirSync(voiceStateDirname(VOICE_STATE_FILE), { recursive: true })\n"
        "    writeFileSync(VOICE_STATE_FILE, JSON.stringify({ status, label }))\n"
        "  } catch {}\n"
        "}\n"
        "\n"
        "function initialMutedFromStateFile() {\n"
        "  try {\n"
        "    if (!existsSync(VOICE_STATE_FILE)) return true\n"
        "    const data = JSON.parse(readFileSync(VOICE_STATE_FILE, 'utf8'))\n"
        "    return data.status !== 'listening'\n"
        "  } catch {\n"
        "    return true\n"
        "  }\n"
        "}\n",
        1,
    ),
    # 3. Initial muted comes from the state file; publish it immediately.
    (
        "  let inputSampleRate = health.realtimeInputSampleRate || 16000\n"
        "  let muted = false\n",
        "  let inputSampleRate = health.realtimeInputSampleRate || 16000\n"
        "  let muted = initialMutedFromStateFile()\n"
        "  writeVoiceState(muted ? 'muted' : 'listening',\n"
        "                  muted ? 'Microphone muted' : 'Microphone listening')\n",
        1,
    ),
    # 4. setMuted: write the real state on every toggle.
    (
        "  const setMuted = value => {\n"
        "    muted = value\n"
        "    if (muted) {\n"
        "      setCaptureEnabled(false)\n"
        "      setStatus('麦克风已静音 · 语音回复保持开启')\n",
        "  const setMuted = value => {\n"
        "    muted = value\n"
        "    writeVoiceState(muted ? 'muted' : 'listening',\n"
        "                    muted ? 'Microphone muted' : 'Microphone listening')\n"
        "    if (muted) {\n"
        "      setCaptureEnabled(false)\n"
        "      setStatus('麦克风已静音 · 语音回复保持开启')\n",
        1,
    ),
    # 5. VOICE_OWNERSHIP busy: the TUI mutes itself -> reflect that in the file.
    (
        "        } else {\n"
        "          muted = true\n"
        "          print(style(`[语音正由${holder}使用]`, 'yellow'))\n"
        "        }\n",
        "        } else {\n"
        "          muted = true\n"
        "          writeVoiceState('muted', 'Voice in use by another client')\n"
        "          print(style(`[语音正由${holder}使用]`, 'yellow'))\n"
        "        }\n",
        1,
    ),
    # 6. VOICE_DEACTIVATED: same.
    (
        "    if (event.type === GatewayServerEvent.VOICE_DEACTIVATED) {\n"
        "      muted = true\n"
        "      setCaptureEnabled(false)\n",
        "    if (event.type === GatewayServerEvent.VOICE_DEACTIVATED) {\n"
        "      muted = true\n"
        "      writeVoiceState('muted', 'Voice switched to another window')\n"
        "      setCaptureEnabled(false)\n",
        1,
    ),
    # 7. Re-assert the mute to the gateway when ownership becomes active, so a
    #    stale gateway session never reopens the mic behind a muted TUI.
    (
        "    if (event.type === GatewayServerEvent.VOICE_OWNERSHIP) {\n"
        "      if (event.state === 'active') {\n"
        "        everOwnedVoice = true\n"
        "        startMicrophone()\n",
        "    if (event.type === GatewayServerEvent.VOICE_OWNERSHIP) {\n"
        "      if (event.state === 'active') {\n"
        "        everOwnedVoice = true\n"
        "        if (muted && socket?.readyState === WebSocket.OPEN) {\n"
        "          socket.send(microphoneControlEvent(true))\n"
        "        }\n"
        "        startMicrophone()\n",
        1,
    ),
    # 8. Exit: mark the assistant stopped so the icon does not stay stale.
    (
        "  close = () => {\n"
        "    cleanup()\n"
        "    if (socket?.readyState < WebSocket.CLOSING) socket.close()\n"
        "  }\n",
        "  close = () => {\n"
        "    writeVoiceState('stopped', 'Voice assistant stopped')\n"
        "    cleanup()\n"
        "    if (socket?.readyState < WebSocket.CLOSING) socket.close()\n"
        "  }\n",
        1,
    ),
]


def main() -> int:
    path = find_tui()
    text = path.read_text()
    applied = 0
    for old, new, expect in REPLACEMENTS:
        count = text.count(old)
        if count == 0:
            print(f"SKIP (anchor missing): {old.splitlines()[0][:70]}")
            continue
        if count > expect:
            raise SystemExit(
                f"anchor ambiguous ({count}x): {old.splitlines()[0][:70]}\n"
                "the installed TUI differs from the patch; refusing to guess.")
        text = text.replace(old, new, 1)
        applied += 1
    path.write_text(text)
    print(f"patched {path}: {applied}/{len(REPLACEMENTS)} replacements applied")
    return 0 if applied == len(REPLACEMENTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())