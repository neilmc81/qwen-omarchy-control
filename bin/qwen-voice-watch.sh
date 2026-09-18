#!/usr/bin/env bash
# Keep the Omarchy bar's qwen.voice indicator in sync with the real microphone.
#
# The stock qwen-audio-agent TUI does not publish its mute state (see
# bin/qwen-voice-state). This polls the hidden TUI's visible output and writes
# $XDG_RUNTIME_DIR/qwen-voice/state.json only when the state actually changes,
# so the bar can watch the file without churn.
#
# Run as a transient systemd user unit by qwen-voice-toggle.sh, which stops it
# when the TUI goes away. It exits on its own once the session is gone, and
# publishes "stopped" first.
set -u

SESSION="${QWEN_VOICE_SESSION:-qwen-voice}"
ROOT="$(dirname "$(readlink -f "$0")")"
STATE_TOOL="$ROOT/qwen-voice-state"

while tmux has-session -t "$SESSION" 2>/dev/null; do
  "$STATE_TOOL" --quiet
  sleep 0.5
done

# The session is gone: say so plainly instead of leaving a stale "listening".
"$STATE_TOOL" --quiet
exit 0
