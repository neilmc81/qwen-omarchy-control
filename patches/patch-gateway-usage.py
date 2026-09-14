#!/usr/bin/env python3
"""Apply the gateway patch that records per-response token usage.

DashScope's OpenAI-compatible realtime API reports a `usage` object on every
`response.done` event (input/output tokens, audio tokens, characters). The
gateway receives these events but discards the usage. This patch appends one
JSON line per completed response to a usage journal so the cost collector
(qwen-cost-update) can aggregate real consumption.

Journal path: $QWEN_AUDIO_USAGE_FILE, default ~/.config/qwaudio/state/usage.jsonl

Run after reinstalling/upgrading qwen-audio-agent. Idempotent.
"""

from __future__ import annotations

import sys
from pathlib import Path


def find_file() -> Path:
    import subprocess
    try:
        prefix = subprocess.run(
            ["npm", "prefix", "-g"], capture_output=True, text=True, check=True,
        ).stdout.strip()
        candidate = Path(prefix) / "lib/node_modules/qwen-audio-agent" \
            / "server/src/voice/realtime-provider.mjs"
        if candidate.exists():
            return candidate
    except Exception:
        pass
    raise SystemExit("could not locate server/src/voice/realtime-provider.mjs")


REPLACEMENTS: list[tuple[str, str, int]] = [
    # 1. fs import for the append helper.
    (
        "import WebSocket from 'ws'\n"
        "import { randomUUID } from 'node:crypto'\n",
        "import WebSocket from 'ws'\n"
        "import { randomUUID } from 'node:crypto'\n"
        "import { appendFileSync, mkdirSync } from 'node:fs'\n",
        1,
    ),
    # 2. Capture usage on every completed response, before the event fan-out.
    (
        "      this.handleLifecycle(event)\n"
        "      this.onEvent?.(event)\n",
        "      this.handleLifecycle(event)\n"
        "      if (event.type === 'response.done') this.recordResponseUsage(event)\n"
        "      this.onEvent?.(event)\n",
        1,
    ),
    # 3. The recorder method, just before updateSession().
    (
        "  updateSession() {\n",
        "  recordResponseUsage(event) {\n"
        "    const usage = event.response?.usage\n"
        "    if (!usage || typeof usage !== 'object') return\n"
        "    const FIELDS = [\n"
        "      'input_tokens', 'output_tokens', 'total_tokens',\n"
        "      'input_audio_tokens', 'output_audio_tokens',\n"
        "      'input_text_characters', 'output_text_characters',\n"
        "      'output_audio_characters',\n"
        "    ]\n"
        "    const picked = { model: this.provider?.model?.() || undefined }\n"
        "    let hit = false\n"
        "    for (const key of FIELDS) {\n"
        "      if (usage[key] !== undefined) {\n"
        "        picked[key] = usage[key]\n"
        "        hit = true\n"
        "      }\n"
        "    }\n"
        "    if (!hit) return\n"
        "    try {\n"
        "      const home = process.env.HOME || '/tmp'\n"
        "      const path = process.env.QWEN_AUDIO_USAGE_FILE\n"
        "        || `${home}/.config/qwaudio/state/usage.jsonl`\n"
        "      mkdirSync(path.slice(0, path.lastIndexOf('/')), { recursive: true })\n"
        "      appendFileSync(path, JSON.stringify({\n"
        "        at: new Date().toISOString(),\n"
        "        ...picked,\n"
        "      }) + '\\n')\n"
        "    } catch {}\n"
        "  }\n"
        "\n"
        "  updateSession() {\n",
        1,
    ),
]


def main() -> int:
    path = find_file()
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
                "installed file differs from the patch; refusing to guess.")
        text = text.replace(old, new, 1)
        applied += 1
    path.write_text(text)
    print(f"patched {path}: {applied}/{len(REPLACEMENTS)} replacements applied")
    return 0 if applied == len(REPLACEMENTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())