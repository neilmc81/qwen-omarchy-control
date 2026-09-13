# qwen-omarchy-control

Low-latency, voice-controlled AI assistant on Omarchy (Arch + Hyprland + PipeWire).
Built around **Qwen Audio Agent** (Alibaba/DashScope realtime voice) with **Hermes**
as the delegated coding backend, and a small local **desktop controller** for instant,
safe desktop commands.

```
Microphone
   │
   ▼
qwen-audio-agent (Realtime, DashScope Qwen Audio 3.0 Realtime Flash)
   │
   ├── simple desktop command ──► local desktop controller (MCP stdio) ──► desktop action
   │                                     (hyprctl / omarchy / wpctl, no shell)
   │
   └── complex task ──► Hermes (ACP) ──► its own tools/MCP/skills/auth ──► result -> voice
```

## Architecture

| Piece | What it is | Where |
| --- | --- | --- |
| qwen-audio-agent | realtime voice runtime (gateway + TUI) | global npm package (`/home/neil/.local/share/mise/installs/node/26.8.1/lib/node_modules/qwen-audio-agent`) |
| Desktop controller | structured, allowlisted operations | `~/.local/share/qwen-omarchy-control/` |
| MCP server | stdio JSON-RPC exposing the controller to the voice frontend | `bin/desktop-mcp` |
| Voice frontend model | `qwen-audio-3.0-realtime-flash` via DashScope | `~/.config/qwaudio/config.env` |
| Coding backend | Hermes via native ACP (`hermes acp`), reuses `~/.hermes` model/provider/auth | `AGENT_PROTOCOL=hermes` |

## Quick commands

```bash
# Status / logs
qwenaudio gateway status
~/.local/share/qwen-omarchy-control/bin/qwen-logs.sh service 50

# Start / stop / restart the gateway (user service, no root)
qwenaudio gateway start
qwenaudio gateway stop
qwenaudio gateway restart

# Talk (TUI in a terminal), requires a running gateway:
qwenaudio tui

# Desktop controller CLI (also testable without voice)
~/.local/share/qwen-omarchy-control/bin/desktop-control ops
~/.local/share/qwen-omarchy-control/bin/desktop-control run list_windows --json
~/.local/share/qwen-omarchy-control/bin/desktop-control run switch_workspace number=3
```

## Hotkey

`SUPER + SHIFT + V` opens/focuses the Qwen TUI and toggles the microphone
(push-to-talk / toggle listening). Defined in `~/.config/hypr/bindings.lua`
under a clearly commented block `[qwen-omarchy-control]`. Revert by deleting
that block.

Backups of configs before edits:
- `~/.config/hypr/bindings.lua.bak-qwen-<timestamp>`
- `~/.config/qwaudio/config.env.bak-*`

## Wake word

The Qwen **Desktop app** ships a local, on-device wake word (sherpa-onnx,
"你好千问", ~33 MB model) that runs only while the orb is asleep and never
uploads audio for detection. As of the installed version the wake detector is
**only available in the Desktop app**, not the CLI/TUI, so the reliable path here
is the `SUPER + SHIFT + V` toggle; the Desktop app can be built/run for wake-word
use (see **Build the Desktop app** below).

## Desktop operations

Exposed to the voice model through the frontend MCP client (fast path, no coding
backend involved), matching the level policy:

- **Level 1 (immediate):** `get_active_window`, `list_windows`,
  `list_workspaces`, `get_monitors`, `switch_workspace`, `focus_window`,
  `launch_app`, `set_volume`, `volume_up`, `volume_down`, `mute_audio`,
  `unmute_audio`, `get_audio_status`, `get_system_status`
- **Level 2 (careful):** `move_active_window_to_workspace`,
  `close_active_window`, `open_url`

Level 3 operations (file deletion, package removal, sudo, shutdown, killing
processes, sending messages, entering passwords, arbitrary shell) are **not**
implemented on the voice path. They belong to the coding backend (Hermes),
which keeps its own per-action permission prompts.

## Safety policy

- No generic `shell(...)` tool exists for the voice frontend.
- The controller builds argv lists only; never `shell=True`.
- `QWEN_AUDIO_AGENT_BACKEND_PERMISSION_MODE=native` (Hermes asks before deletes,
  writes, and shell commands; the gateway forwards requests as-is).
- Unknown tools fail closed.

## Application discovery

`launch_app` resolves installed desktop entries from the XDG application
directories (and `~/.local/share/applications`) plus built-in routes:
`terminal`, `browser`, `files`, `editor`. Chrome = `google-chrome`,
Spotify = `spotify`, etc. Nothing is hard-coded to a machine-specific path.

## Qwen configuration location

`~/.config/qwaudio/config.env` (mode 0600). Contains `DASHSCOPE_API_KEY`,
`QWEN_AUDIO_REALTIME_MODEL`, `QWEN_AUDIO_REALTIME_BASE_URL`, `AGENT_PROTOCOL`,
`QWEN_AUDIO_AGENT_BACKEND_PERMISSION_MODE`, and the frontend MCP path.

## Region / endpoint

International DashScope (Singapore ap-southeast-1) realtime endpoint:
`wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime` — set via
`QWEN_AUDIO_REALTIME_BASE_URL`. Requires an API key issued for the
international account. To switch back to the mainline endpoint remove that line.

## How to update

```bash
npm install -g qwen-audio-agent@latest     # then:
qwenaudio gateway restart
cd ~/.local/share/qwen-omarchy-control && git pull
```

## How to stop it

- Hotkey: press `SUPER + SHIFT + V` (mutes plumbing, or closes the TUI).
- Gateway: `qwenaudio gateway stop`.
- Auto-restart on boot: `systemctl --user disable --now qwen-audio-agent-gateway.service`.

## How to uninstall everything we added

1. Remove the Hyprland binding block marked `[qwen-omarchy-control]` in
   `~/.config/hypr/bindings.lua` and `hyprctl reload`.
2. `qwenaudio gateway uninstall` (removes the user service).
3. `npm uninstall -g qwen-audio-agent`.
4. Delete `~/.config/qwaudio/` (your chat state; keep backups if wanted) and
   restore `config.env` backup if you want to keep it.
5. Run `~/.local/share/qwen-omarchy-control/uninstall.sh` to remove the desktop
   controller project (optionally `--purge` for the config).
6. Optional: restore old omarchy-voice from
   `~/.local/share/qwen-omarchy-control-backups/old-system-*` if you ever want it back.

Nothing in the uninstall touches `~/.hermes`, `~/.codex`, or the Hermes gateway.

## Known limitations

- Linux TUI is half-duplex by default (mic paused while the assistant speaks).
  Full duplex needs `qwenaudio tui --audio-mode full` and headphones (no echo
  cancellation). Do not switch to full duplex on speakers.
- Interruption in the TUI is `/interrupt` (or `x`) in half-duplex mode.
- The on-device wake word only exists in the Desktop app build (see above).
- The gateway must be running for the hotkey's TUI to connect.

## Measured latency

Filled in from the acceptance run; see `INSTALL_LOG.md`.

## Files generated / modified during setup

- `~/.local/share/qwen-omarchy-control/` (this repo)
- `~/.config/qwaudio/{config.env,ASSISTANT.md,state,data,logs,tui}` (Qwen)
- `~/.config/systemd/user/qwen-audio-agent-gateway.service` (gateway service; a
  generated file with one local fix to `WorkingDirectory=` quoting)
- `~/.config/hypr/bindings.lua` (voice binding, backed up as `*.bak-qwen-*`)
- Old `omarchy-voice` moved to `~/.local/share/qwen-omarchy-control-backups/old-system-*`

## Build the Desktop app (orb + wake word)

```bash
git clone --depth 1 https://github.com/QwenAudio/qwen-audio-agent /tmp/qa
cd /tmp/qa && npm install
npm run desktop:build:linux       # produces dist/desktop/*.AppImage and *.deb
```
Run the AppImage; it shares `~/.config/qwaudio` with the CLI. Note the desktop
app maintains a separate gateway _runtime_ (`state/desktop`), so run either the
CLI gateway or the Desktop-oned, not both.