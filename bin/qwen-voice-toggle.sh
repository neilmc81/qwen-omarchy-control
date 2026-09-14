#!/usr/bin/env bash
# qwen-voice toggle: push-to-talk with NO visible window.
#
# Runs the Qwen TUI inside a hidden, detached tmux session. The hotkey toggles
# the microphone ('/m' inside the TUI) without ever showing a terminal window.
#
# The TUI itself owns the state file at $XDG_RUNTIME_DIR/qwen-voice/state.json:
# it writes its REAL mute state on every transition, so the bar indicator always
# matches whether the mic is actually listening. This script only sends '/m'
# and then reads back whatever the TUI actually did - it never invents state.
# See patches/patch-tui.py for the TUI side of that contract.
#
# First press: starts the TUI (muted) in the background.
# Later presses: toggle the microphone (listen <-> muted).
# To see the TUI:   tmux attach -t qwen-voice      (detach again with Ctrl-b d)
set -u

SESSION="qwen-voice"
STATE_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/qwen-voice"
STATE_FILE="$STATE_DIR/state.json"

read_state() {
  python3 -c "import json,sys,os
try: print(json.load(open(os.path.expandvars('$STATE_FILE')))['status'])
except Exception: print('')" 2>/dev/null
}

# Make sure `qwenaudio` is reachable inside the tmux session.
NPM_BIN="$(npm prefix -g 2>/dev/null)/bin"
case ":$PATH:" in *":$NPM_BIN:"*) ;; *) export PATH="$PATH:$NPM_BIN" ;; esac

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  # Start the TUI hidden. It begins muted (push-to-talk default) and writes the
  # state file itself on start, so the icon settles without our help.
  tmux new-session -d -s "$SESSION" -x 110 -y 32 "qwenaudio tui" 2>/dev/null
  before="$(read_state)"
  for _ in $(seq 1 30); do
    now="$(read_state)"
    [ -n "$now" ] && [ "$now" != "$before" ] && break
    sleep 0.5
  done
  state="$(read_state)"; [ -n "$state" ] || state="muted"
  if [ "$state" = "muted" ]; then
    notify-send -a qwen-voice "Qwen Voice" "Ready in background (muted). Press again to listen." 2>/dev/null &
  else
    notify-send -a qwen-voice "Qwen Voice" "Listening." 2>/dev/null &
  fi
  exit 0
fi

before="$(read_state)"
tmux send-keys -t "$SESSION" "/m" Enter >/dev/null 2>&1
# The TUI writes the new state; give it a few seconds.
for _ in $(seq 1 12); do
  now="$(read_state)"
  [ -n "$now" ] && [ "$now" != "$before" ] && break
  sleep 0.25
done
state="$(read_state)"; [ -n "$state" ] || state="$before"
case "$state" in
  listening) notify-send -a qwen-voice "Qwen Voice" "Microphone listening" 2>/dev/null & ;;
  muted)     notify-send -a qwen-voice "Qwen Voice" "Microphone muted" 2>/dev/null & ;;
  stopped)   notify-send -a qwen-voice "Qwen Voice" "Voice assistant stopped" 2>/dev/null & ;;
esac
exit 0