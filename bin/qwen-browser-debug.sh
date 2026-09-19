#!/usr/bin/env bash
# Enable typed browser control: restart Chrome with a DevTools endpoint.
#
# WHY THIS IS NEEDED
# Chrome exposes no accessibility tree, so the precise element tools cannot see
# inside it and fall back to OCR. cua-driver can bind to Chrome's own DevTools
# endpoint instead and address real DOM elements (this is what browser_read /
# browser_click / browser_type use). Chrome only opens that endpoint when it is
# launched with --remote-debugging-port.
#
# WHAT THIS DOES
# Adds --remote-debugging-port=9222 to ~/.config/chrome-flags.conf, which
# /usr/bin/google-chrome-stable already reads (Omarchy ships this). Chrome must
# be restarted for it to take effect.
#
# SECURITY - READ THIS
# A loopback DevTools endpoint lets any local process that can reach port 9222
# fully control the browser, including reading cookies and authenticated
# sessions. It listens on 127.0.0.1 only and is not exposed to the network, but
# any program you run as this user can use it. Remove the flag (this script's
# --disable) when you do not want that.
#
# cua-driver must also be started with --grant existing-profile for it to attach
# to a logged-in profile; see README: Typed browser control.
set -u

FLAGS="${XDG_CONFIG_HOME:-$HOME/.config}/chrome-flags.conf"
FLAG_LINE="--remote-debugging-port=9222"

usage() { echo "usage: $0 [--enable|--disable|--status]"; exit 2; }

current() {
  if [ -f "$FLAGS" ]; then
    grep -c -- "$FLAG_LINE" "$FLAGS" 2>/dev/null
  else
    echo 0
  fi
}

case "${1:---status}" in
  --enable)
    if [ ! -f "$FLAGS" ]; then
      echo "no $FLAGS to edit; is Chrome installed the Omarchy way?" >&2
      exit 1
    fi
    if [ "$(current)" -gt 0 ]; then
      echo "already enabled in $FLAGS"
      exit 0
    fi
    cp "$FLAGS" "$FLAGS.bak-$(date +%s)"
    echo "$FLAG_LINE" >> "$FLAGS"
    echo "enabled in $FLAGS (backup saved)"
    echo "Restart Chrome to apply:  pkill -f 'google-chrome' ; google-chrome-stable &"
    echo "Note: the machine-wide cua-driver daemon also needs '--grant existing-profile'."
    ;;
  --disable)
    if [ -f "$FLAGS" ] && [ "$(current)" -gt 0 ]; then
      sed -i "\|^${FLAG_LINE}$|d" "$FLAGS"
      echo "disabled in $FLAGS; restart Chrome to apply"
    else
      echo "not enabled in $FLAGS"
    fi
    ;;
  --status)
    if [ "$(current)" -gt 0 ]; then
      echo "DevTools endpoint flag: ENABLED"
      if ss -ltn 2>/dev/null | grep -q ':9222'; then
        echo "port 9222: listening"
      else
        echo "port 9222: not listening (restart Chrome?)"
      fi
    else
      echo "DevTools endpoint flag: not set (typed browser control unavailable)"
    fi
    ;;
  *) usage ;;
esac
