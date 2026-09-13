#!/usr/bin/env bash
# qwen-voice toggle: push-to-talk with NO visible window.
#
# Runs the Qwen TUI inside a hidden, detached tmux session. The hotkey toggles
# the microphone ('m' = mute/unmute inside the TUI) without ever showing a
# terminal window. A desktop notification reports the state.
#
# First press: starts the TUI (muted) in the background.
# Later presses: toggle the microphone (listen <-> muted).
# To see the TUI:   tmux attach -t qwen-voice      (detach again with Ctrl-b d)
set -u

SESSION="qwen-voice"

# Make sure `qwenaudio` is reachable inside the tmux session.
NPM_BIN="$(npm prefix -g 2>/dev/null)/bin"
case ":$PATH:" in *":$NPM_BIN:"*) ;; *) export PATH="$PATH:$NPM_BIN" ;; esac

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  # Start the TUI hidden, then mute it so we are push-to-talk by default.
  tmux new-session -d -s "$SESSION" -x 110 -y 32 "qwenaudio tui" 2>/dev/null
  # Wait for the gateway connection before the first toggle.
  sleep 6
  tmux send-keys -t "$SESSION" "/m" Enter >/dev/null 2>&1
  notify-send -a qwen-voice "Qwen Voice" "Ready in background (muted). Press again to listen." 2>/dev/null &
  exit 0
fi

# Toggle the microphone (the TUI toggles with the /m command, not a bare key).
tmux send-keys -t "$SESSION" "/m" Enter >/dev/null 2>&1
notify-send -a qwen-voice "Qwen Voice" "Microphone toggled" 2>/dev/null &
exit 0