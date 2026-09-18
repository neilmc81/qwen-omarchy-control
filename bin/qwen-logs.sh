#!/usr/bin/env bash
# Privacy-conscious log viewer for the Qwen voice assistant.
# Masks likely API keys before printing.
mask() { sed -E 's/(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]+/\1***/g; s/(Bearer )[A-Za-z0-9._-]{4,}/\1***/g; s/(DASHSCOPE_API_KEY[=:][[:space:]]*)[^ ][^ ]*/\1***/Ig'; }
# qwen-audio-agent moved its logs from ~/.config/qwaudio/logs/ to
# ~/.config/qwaudio/state/logs/ (QWEN_AUDIO_LOG_* / QWAUDIO_DATA_DIR). Read the
# live location, falling back to the legacy path for older installs.
LOG_DIR="$HOME/.config/qwaudio/state/logs"
[ -d "$LOG_DIR" ] || LOG_DIR="$HOME/.config/qwaudio/logs"

case "${1:-gateway}" in
  service) journalctl --user -u qwen-audio-agent-gateway.service -n "${2:-50}" --no-pager | mask ;;
  gateway) tail -n "${2:-80}" "$LOG_DIR/gateway.log" | mask ;;
  all)     tail -n "${2:-80}" -f "$LOG_DIR/gateway.log" "$LOG_DIR/cli.log" | mask ;;
  *) echo "usage: qwen-logs {service|gateway|all} [lines]"; exit 1 ;;
esac
