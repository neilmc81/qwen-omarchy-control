#!/usr/bin/env bash
# Privacy-conscious log viewer for the Qwen voice assistant.
# Masks likely API keys before printing.
mask() { sed -E 's/(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]+/\1***/g; s/(Bearer )[A-Za-z0-9._-]{4,}/\1***/g; s/(DASHSCOPE_API_KEY[=:][[:space:]]*)[^ ][^ ]*/\1***/Ig'; }
case "${1:-gateway}" in
  service) journalctl --user -u qwen-audio-agent-gateway.service -n "${2:-50}" --no-pager | mask ;;
  gateway) tail -n "${2:-80}" "$HOME/.config/qwaudio/logs/gateway.log" | mask ;;
  all)     tail -n "${2:-80}" -f "$HOME/.config/qwaudio/logs/gateway.log" "$HOME/.config/qwaudio/logs/cli.log" | mask ;;
  *) echo "usage: qwen-logs {service|gateway|all} [lines]"; exit 1 ;;
esac
