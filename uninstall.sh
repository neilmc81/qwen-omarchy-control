#!/usr/bin/env bash
# Uninstall the qwen-omarchy-control integration only.
#
# Removes:
#   - the Hyprland voice binding (restores the backup, if present)
#   - the frontend MCP config and the desktop-controller project
#   - (optionally, with --purge) runtime/config leftovers
#
# Does NOT touch: ~/.hermes, ~/.codex, ~/.config/qwaudio (Qwen account/chat
# config), DashScope credentials, or unrelated system files.
set -eu

VERSION="0.1.0"
PROJECT="$HOME/.local/share/qwen-omarchy-control"
BACKUP_BASE="$HOME/.local/share/qwen-omarchy-control-backups"
PURGE=0
if [ "${1:-}" = "--purge" ]; then PURGE=1; fi

confirm() {
  printf "%s [y/N] " "$1"
  read -r ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ]
}

echo "qwen-omarchy-control uninstaller v$VERSION"
echo "This removes the desktop-controller integration (not your Qwen config, Hermes, or Codex)."

if ! confirm "Continue?"; then echo "aborted"; exit 1; fi

# 1. Remove the Hyprland voice binding and restore its backup.
BINDINGS="$HOME/.config/hypr/bindings.lua"
if [ -f "$BINDINGS" ] && grep -q 'qwen-voice-toggle\|qwen-omarchy-control' "$BINDINGS"; then
  bak="$(ls -t "$HOME/.config/hypr"/bindings.lua.bak-qwen-* 2>/dev/null | head -n1 || true)"
  if [ -n "$bak" ]; then
    cp "$BINDINGS" "$HOME/.config/hypr/bindings.lua.pre-qwen-uninstall-$(date +%s)"
    cp "$bak" "$BINDINGS"
    echo "Restored $BINDINGS from $bak"
  else
    echo "No binding backup found; you must remove the [qwen-omarchy-control] block manually."
  fi
  command -v hyprctl >/dev/null && hyprctl reload >/dev/null 2>&1 && echo "Hyprland reloaded"
fi

# 2. Remove the project.
if [ -d "$PROJECT" ]; then
  if [ "$PURGE" = "1" ]; then
    rm -rf "$PROJECT"
    echo "Removed $PROJECT"
  else
    mv "$PROJECT" "$BACKUP_BASE/qwen-omarchy-control-uninstalled-$(date +%s)"
    echo "Moved $PROJECT to $BACKUP_BASE (recoverable). Use --purge to delete outright."
  fi
fi

# 3. Optionally clear config/env copies inside qwaudio (kept by default).
if [ "$PURGE" = "1" ]; then
  echo "Purging frontend MCP reference from ~/.config/qwaudio/config.env"
  sed -i '/QWEN_AUDIO_FRONTEND_MCP_CONFIG=/d' "$HOME/.config/qwaudio/config.env" 2>/dev/null || true
  echo "Note: qwen-audio-agent itself, its gateway service and Qwen config are NOT removed."
  echo "To remove those too, run:"
  echo "  qwenaudio gateway uninstall && npm uninstall -g qwen-audio-agent"
  echo "(this will also remove your Qwen account/chat config unless you back up ~/.config/qwaudio)"
fi

echo
echo "Done. To fully remove qwen-audio-agent itself, see README.md 'How to uninstall'."
exit 0