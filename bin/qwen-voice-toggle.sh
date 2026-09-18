#!/usr/bin/env bash
# qwen-voice toggle: push-to-talk with NO visible window, and no patched package.
#
# Runs the stock qwen-audio-agent TUI inside a hidden, detached tmux session.
# The hotkey toggles the microphone ('/m' inside the TUI) without ever showing a
# terminal window.
#
# The stock TUI starts with the microphone OPEN. This used to be fixed by a
# patch that made it read a state file at startup; that patch is gone. Instead,
# first press sends '/m' immediately and then *requires* the TUI to report
# itself muted. If it cannot confirm that within a few seconds, the session is
# killed rather than left with an open microphone.
#
# State for the bar is published by bin/qwen-voice-state from the TUI's own
# visible output (see qwen-voice-watch.sh), which is the single source of truth.
#
# To see the TUI:   tmux attach -t qwen-voice      (detach again with Ctrl-b d)
set -u

SESSION="${QWEN_VOICE_SESSION:-qwen-voice}"
ROOT="$(dirname "$(readlink -f "$0")")"
STATE_TOOL="$ROOT/qwen-voice-state"
WATCHER="$ROOT/qwen-voice-watch.sh"

notify() { notify-send -a qwen-voice "Qwen Voice" "$1" 2>/dev/null & }

# Make sure `qwenaudio` is reachable inside the tmux session.
NPM_BIN="$(npm prefix -g 2>/dev/null)/bin"
case ":$PATH:" in *":$NPM_BIN:"*) ;; *) export PATH="$PATH:$NPM_BIN" ;; esac

# Raw state: listening | muted | unknown | stopped. `unknown` must never be
# read as a confirmation that the microphone is shut.
raw_state() { "$STATE_TOOL" --state 2>/dev/null; }

if tmux has-session -t "$SESSION" 2>/dev/null; then
  # Already running: flip the microphone. The watcher publishes the result.
  before="$(raw_state)"
  tmux send-keys -t "$SESSION" "/m" Enter >/dev/null 2>&1
  for _ in $(seq 1 24); do
    sleep 0.25
    "$STATE_TOOL" --quiet
    now="$(raw_state)"
    [ -n "$now" ] && [ "$now" != "$before" ] && break
  done
  case "$(raw_state)" in
    listening) notify "Microphone listening" ;;
    muted)     notify "Microphone muted" ;;
    *)         notify "Voice state unavailable" ;;
  esac
  exit 0
fi

# Fresh start. The stock TUI opens the microphone; mute it and require the TUI
# to say so before we trust the session. `/m` is re-sent while the pane still
# reports listening, because a keystroke sent before the TUI has wired up its
# input reader is simply dropped.
tmux new-session -d -s "$SESSION" -x 110 -y 32 "qwenaudio tui" 2>/dev/null

muted_confirmed=0
for attempt in $(seq 1 60); do
  sleep 0.25
  "$STATE_TOOL" --quiet
  state="$(raw_state)"
  # Exactly "muted" confirms it. "unknown" (nothing rendered yet) does not.
  if [ "$state" = "muted" ]; then muted_confirmed=1; break; fi
  if [ "$state" = "listening" ]; then
    tmux send-keys -t "$SESSION" "/m" Enter >/dev/null 2>&1
  fi
done

if [ "$muted_confirmed" != "1" ]; then
  # Fail closed: an unconfirmed microphone must not stay open.
  tmux kill-session -t "$SESSION" 2>/dev/null
  "$STATE_TOOL" --quiet
  notify "Could not confirm muted; voice assistant stopped."
  exit 1
fi

# Watch for changes. Run it as a transient user unit so it is fully detached
# from whoever pressed the key (and from the shell), and cannot accumulate one
# copy per press.
start_watcher() {
  if systemctl --user is-active --quiet qwen-voice-watch.service 2>/dev/null; then
    return 0
  fi
  if command -v systemd-run >/dev/null 2>&1; then
    systemd-run --user --quiet --collect --unit=qwen-voice-watch \
      --description="Qwen voice microphone-state watcher" \
      "$WATCHER" >/dev/null 2>&1 && return 0
  fi
  # No systemd available: fall back to a detached process.
  setsid "$WATCHER" >/dev/null 2>&1 &
  return 0
}
start_watcher
"$STATE_TOOL" --quiet
notify "Ready in background (muted). Press again to listen."
exit 0
