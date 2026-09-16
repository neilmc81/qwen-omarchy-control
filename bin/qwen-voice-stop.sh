#!/usr/bin/env bash
# Physical stop for the Qwen voice assistant (stock build, no overlay/bridge).
#
# The codex overlay's local-stop socket (qwen-stop.sock) no longer exists after
# the bridge was removed, so this key would be dead. Instead it sends the stock
# TUI's own interrupt command into its tmux session, which cancels any reply
# that is currently being spoken and resumes listening.
#
# It never toggles the microphone (that is SUPER+SHIFT+V).
set -u

SESSION="${QWEN_VOICE_SESSION:-qwen-voice}"

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  # Voice assistant is not running: nothing to stop; stay silent for a hotkey.
  exit 0
fi

# Stock TUI command: /interrupt cancels the current spoken reply.
tmux send-keys -t "$SESSION" "/interrupt" Enter >/dev/null 2>&1
exit 0
