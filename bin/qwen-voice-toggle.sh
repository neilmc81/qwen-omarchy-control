#!/usr/bin/env bash
# qwen-voice toggle: push-to-talk with NO visible window.
#
# Runs the Qwen TUI inside a hidden, detached tmux session. The hotkey toggles
# the microphone ('/m' = mute/unmute inside the TUI) without ever showing a
# terminal window. A desktop notification reports the state, and a small JSON
# state file drives the bar indicator (green = listening, dark = muted/stopped).
#
# First press: starts the TUI (muted) in the background.
# Later presses: toggle the microphone (listen <-> muted).
# To see the TUI:   tmux attach -t qwen-voice      (detach again with Ctrl-b d)
set -u

SESSION="qwen-voice"
STATE_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/qwen-voice"
STATE_FILE="$STATE_DIR/state.json"

write_state() {
  mkdir -p "$STATE_DIR"
  printf '{"status":"%s","label":"%s"}\n' "$1" "$2" > "$STATE_FILE"
  chmod 600 "$STATE_FILE"
}

# Make sure `qwenaudio` is reachable inside the tmux session.
NPM_BIN="$(npm prefix -g 2>/dev/null)/bin"
case ":$PATH:" in *":$NPM_BIN:"*) ;; *) export PATH="$PATH:$NPM_BIN" ;; esac

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  # Start the TUI hidden, then mute it so we are push-to-talk by default.
  tmux new-session -d -s "$SESSION" -x 110 -y 32 "qwenaudio tui" 2>/dev/null
  # Wait for the gateway connection before the first toggle.
  sleep 6
  tmux send-keys -t "$SESSION" "/m" Enter >/dev/null 2>&1
  write_state "muted" "Ready in background (muted)"
  notify-send -a qwen-voice "Qwen Voice" "Ready in background (muted). Press again to listen." 2>/dev/null &
  exit 0
fi

# Flip the tracked state, then toggle the microphone (the TUI uses /m).
current="$(python3 -c "import json,sys,os
try: print(json.load(open(os.path.expandvars('$STATE_FILE')))['status'])
except Exception: print('')" 2>/dev/null)"
if [ "$current" = "listening" ]; then
  next="muted"
else
  next="listening"
fi
tmux send-keys -t "$SESSION" "/m" Enter >/dev/null 2>&1
write_state "$next" "Microphone $next"
notify-send -a qwen-voice "Qwen Voice" "Microphone $next" 2>/dev/null &
exit 0