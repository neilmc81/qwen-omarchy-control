#!/usr/bin/env bash
# qwen-voice toggle: push-to-talk / toggle-listening hotkey.
#
#  - If the Qwen TUI is already open (window class qwen-voice), focus it and
#    press 'm' to toggle the microphone (mute <-> listening).
#  - Otherwise open the Qwen TUI in a dedicated foot terminal (app-id=qwen-voice)
#    so a second press can toggle it.
#
# Requires a running gateway (qwenaudio gateway status) and a Wayland session.
set -u

SIG=""
for d in "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/hypr"/*/; do
  if [ -S "${d}.socket.sock" ]; then SIG="$(basename "$d")"; break; fi
done
if [ -z "$SIG" ]; then
  notify-send -a qwen-voice "Qwen Voice" "Hyprland not found" 2>/dev/null
  exit 1
fi
export HYPRLAND_INSTANCE_SIGNATURE="$SIG"

addr="$(hyprctl -j clients | python3 -c '
import json,sys
for c in json.load(sys.stdin):
    if c.get("class") == "qwen-voice" and not c.get("hidden"):
        print(c.get("address"))
        break
' 2>/dev/null)"

if [ -z "$addr" ]; then
  exec setsid foot --app-id=qwen-voice -T "Qwen Voice" qwenaudio tui >/dev/null 2>&1 &
  TUI_PID=$!
  notify-send -a qwen-voice "Qwen Voice" "Opened Qwen TUI; press again to mute" 2>/dev/null &
  exit 0
fi

# Focus the TUI and press 'm' to toggle the mic.
hyprctl dispatch "hl.dsp.focus({ window = \"$addr\" })" >/dev/null 2>&1
sleep 0.25
wtype m >/dev/null 2>&1
notify-send -a qwen-voice "Qwen Voice" "Microphone toggled" 2>/dev/null &
exit 0