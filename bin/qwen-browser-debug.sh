#!/usr/bin/env bash
# Enable typed browser control: give Chrome a DevTools endpoint on the real,
# logged-in profile.
#
# WHY THIS IS NEEDED
# Chrome exposes no accessibility tree, so the precise element tools cannot see
# inside it and fall back to OCR. cua-driver can instead bind to Chrome's own
# DevTools endpoint and address real DOM elements (browser_read / browser_click
# / browser_type / browser_search). Chrome only opens that endpoint when it is
# launched with --remote-debugging-port.
#
# WHY IT IS NOT JUST A FLAG
# Chrome 153 refuses remote debugging on its DEFAULT data directory:
#     "DevTools remote debugging requires a non-default data directory."
# The RemoteDebuggingAllowed policy does NOT lift this (verified: with the
# policy false Chrome says "disallowed by the system admin"; with it true Chrome
# still demands a non-default directory). So Chrome must be given a non-default
# PATH that still contains the real profile. Two ways were tested:
#
#   * a symlinked directory -> TWO Chrome instances, because each path gets its
#     own SingletonLock. This can corrupt the profile. REJECTED, do not use.
#   * a bind mount -> one shared SingletonLock, so a normal launch hands off to
#     the debug instance ("Opening in existing browser session"). Verified.
#
# This script therefore sets up a bind mount of the real profile at a
# non-default path, and points Chrome's flags at that path.
#
# SECURITY - READ THIS
# A loopback DevTools endpoint lets any local process that can reach port 9222
# fully control the browser, including reading cookies and authenticated
# sessions. It listens on 127.0.0.1 only and is never exposed to the network,
# but any program you run as this user can use it. --disable removes it.
#
# cua-driver must also be started with --grant existing-profile for it to attach
# to a logged-in profile; see README: Typed browser control.
#
# OPERATIONAL NOTES (measured)
#   * Chrome's newer `chrome://inspect` remote-debugging toggle is NOT a
#     substitute. With it on and these flags absent, cua-driver refuses with
#     `browser_wrong_target_refused` / `browser_reconnect_exhausted` because
#     that bridge does not expose the classic /json/version endpoint cua uses
#     to prove socket ownership (it returns 404). The flag path here does
#     (200), which is why it is the supported route.
#   * If a prepare call starts refusing after Chrome or its endpoint changed,
#     restart the daemon to clear stale endpoint state:
#       systemctl --user restart cua-driver.service
set -u

CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
REAL_DIR="$CONFIG/google-chrome"          # the real, logged-in profile
ALT_DIR="$CONFIG/google-chrome-qwen"      # non-default path -> bind-mounted alias
FLAGS="$CONFIG/chrome-flags.conf"
PORT=9222
FSTAB="/etc/fstab"
FSTAB_MARK="qwen-omarchy-control: a non-default data-dir"

usage() { echo "usage: $0 [--enable|--disable|--status]"; exit 2; }

mounted() { mountpoint -q "$ALT_DIR" 2>/dev/null; }
flag_set() { grep -q -- "$1" "$FLAGS" 2>/dev/null; }

need_root() {
  if ! sudo -n true 2>/dev/null; then
    echo "This step needs root to create the bind mount (sudo password required)." >&2
    exit 1
  fi
}

enable() {
  [ -d "$REAL_DIR" ] || { echo "no $REAL_DIR - is Chrome installed the Omarchy way?" >&2; exit 1; }

  # 1. The bind mount: non-default path, same real data, shared singleton lock.
  if ! mounted; then
    need_root
    mkdir -p "$ALT_DIR"
    sudo mount --bind "$REAL_DIR" "$ALT_DIR" || {
      echo "bind mount failed" >&2; exit 1; }
    echo "bind-mounted $REAL_DIR -> $ALT_DIR"
  else
    echo "bind mount already active"
  fi

  # 2. Persist it across reboots, but never risk boot: nofail + requires /home.
  if ! sudo -n grep -q "$FSTAB_MARK" "$FSTAB" 2>/dev/null; then
    need_root
    sudo cp "$FSTAB" "$FSTAB.bak-qwen-$(date +%s)"
    sudo tee -a "$FSTAB" >/dev/null <<EOF

# $FSTAB_MARK
# *path* for Chrome, bind-mounted to the real logged-in profile so cua-driver can
# attach a DevTools endpoint. Chrome 153 refuses remote debugging on the default
# profile directory, and no policy overrides that. A bind mount shares the
# singleton lock, so launches hand off to a single process (a symlinked dir
# would fork two and risk corruption). Remove this line and unmount to disable;
# see bin/qwen-browser-debug.sh --disable.
$REAL_DIR  $ALT_DIR  none  bind,nofail,x-systemd.requires=/home  0 0
EOF
    echo "added fstab entry (backup saved)"
  fi

  # 3. The flags. --user-data-dir is REQUIRED: without it a normal launch uses
  # the default path and Chrome refuses the port. It points at the bind mount,
  # which is the real profile.
  [ -f "$FLAGS" ] || { echo "no $FLAGS to edit" >&2; exit 1; }
  local added=0
  if ! flag_set "--remote-debugging-port=$PORT"; then
    flag_set "--remote-debugging-port" || { echo "--remote-debugging-port=$PORT" >> "$FLAGS"; added=1; }
  fi
  if ! flag_set "--user-data-dir=$ALT_DIR"; then
    echo "--user-data-dir=$ALT_DIR" >> "$FLAGS"; added=1
  fi
  [ "$added" = 1 ] && { cp "$FLAGS" "$FLAGS.bak-$(date +%s)" 2>/dev/null || true; echo "updated $FLAGS"; }

  echo
  echo "Restart Chrome to apply:"
  echo "  for p in \$(pgrep -f 'chrome/chrome --ozone'); do kill \$p; done"
  echo "  google-chrome-stable &"
  echo "Then verify:  $0 --status"
  echo "And the cua-driver daemon must have been started with --grant existing-profile."
}

disable() {
  # Flags first, so a restart can't pick them up again.
  if [ -f "$FLAGS" ]; then
    sed -i '\|^--remote-debugging-port=|d; \|^--user-data-dir='"${ALT_DIR//\//\\/}"'$|d' "$FLAGS" 2>/dev/null
    echo "removed debug/user-data-dir flags from $FLAGS"
  fi
  if sudo -n grep -q "$FSTAB_MARK" "$FSTAB" 2>/dev/null; then
    sudo sed -i "\|$FSTAB_MARK|,\|bind,nofail,x-systemd.requires=/home|d" "$FSTAB"
    echo "removed fstab entry"
  fi
  if mounted; then
    sudo umount "$ALT_DIR" 2>/dev/null && echo "unmounted $ALT_DIR" || echo "could not unmount (Chrome running?)"
  fi
  rmdir "$ALT_DIR" 2>/dev/null
  echo "Disabled. Restart Chrome."
}

status() {
  local ok=1
  if mounted; then echo "bind mount:    active ($ALT_DIR -> $REAL_DIR)"; else echo "bind mount:    MISSING"; ok=0; fi
  if flag_set "--remote-debugging-port=$PORT"; then echo "debug flag:    set"; else echo "debug flag:    not set"; ok=0; fi
  if flag_set "--user-data-dir=$ALT_DIR"; then echo "user-data-dir: set"; else echo "user-data-dir: not set"; ok=0; fi
  if ss -ltn 2>/dev/null | grep -q ":$PORT"; then
    echo "port $PORT:      listening"
  else
    echo "port $PORT:      not listening (restart Chrome?)"
    ok=0
  fi
  [ "$ok" = 1 ] && echo "=> typed browser control is READY" || echo "=> typed browser control is NOT ready"
}

case "${1:---status}" in
  --enable)  enable ;;
  --disable) disable ;;
  --status)  status ;;
  *) usage ;;
esac
