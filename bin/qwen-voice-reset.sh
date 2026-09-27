#!/usr/bin/env bash
# Restart the gateway and replace the voice TUI session, preserving mic state.
# Intended for the Qwen bar widget's explicit reset button.
set -euo pipefail

ROOT="$(dirname "$(readlink -f "$0")")"
SESSION="${QWEN_VOICE_SESSION:-qwen-voice}"
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/qwen-voice"
mkdir -p "$RUNTIME"
if [ "${1:-}" != "--locked" ]; then
  # flock's parent holds the lock; -o closes its FD in this script and in the
  # detached TUI, so a completed reset cannot block every later reset.
  if flock -n -E 75 -o "$RUNTIME/reset-v2.lock" "$0" --locked; then
    exit 0
  else
    status=$?
    [ "$status" -eq 75 ] && exit 0  # A reset is already in progress.
    exit "$status"
  fi
fi

notify() { notify-send -a qwen-voice "Qwen Voice" "$1" >/dev/null 2>&1 || true; }
previous="$("$ROOT/qwen-voice-state" --state 2>/dev/null || true)"
notify "Resetting the voice assistant…"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux kill-session -t "$SESSION"
fi
if ! systemctl --user restart qwen-audio-agent-gateway.service; then
  notify "Reset failed: the Gateway did not restart."
  exit 1
fi

ready=0
for _ in $(seq 1 40); do
  if python3 -c 'import json,urllib.request
d=json.load(urllib.request.urlopen("http://127.0.0.1:3101/api/health",timeout=2))
assert d["status"] == "ready" and d["frontendMcp"]["ok"]' >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.5
done
if [ "$ready" -ne 1 ]; then
  notify "Reset failed: the Gateway or desktop tools are not ready."
  exit 1
fi

if ! "$ROOT/qwen-voice-toggle.sh"; then
  notify "Reset failed: the voice client did not start muted."
  exit 1
fi
if [ "$previous" = "listening" ]; then
  "$ROOT/qwen-voice-toggle.sh"
fi

current="$("$ROOT/qwen-voice-state" --state 2>/dev/null || true)"
if [ "$previous" = "listening" ] && [ "$current" != "listening" ]; then
  notify "Reset finished, but the microphone did not resume listening."
  exit 1
fi
notify "Reset complete — microphone ${current:-unknown}."
