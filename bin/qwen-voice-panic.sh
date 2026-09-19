#!/usr/bin/env bash
# Panic stop for computer use: toggle a flag that suppresses all agent input.
#
# The vision tools move the REAL mouse and take focus. While this flag exists,
# click_element refuses to send anything (it raises before touching the
# pointer). Bind it to a key you can hit without looking; the agent also checks
# the flag between every step of a sequence.
#
#   SUPER + SHIFT + ESCAPE   -> set the flag   (press again to clear)
#
# Clearing is deliberately the same key: a stop key you have to find a *second*
# different binding to undo is a worse stop key. The state is announced either
# way so the toggle is never ambiguous.
set -u

FLAG="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/qwen-voice/stop"
mkdir -p "$(dirname "$FLAG")"

if [ -e "$FLAG" ]; then
  rm -f "$FLAG"
  notify-send -a qwen-voice "Qwen computer use" "Resumed. The agent may use the mouse again." 2>/dev/null &
else
  : > "$FLAG"
  notify-send -a qwen-voice -u critical "Qwen computer use STOPPED" \
    "The agent will not touch the mouse or keyboard. Press again to resume." 2>/dev/null &
fi
exit 0
