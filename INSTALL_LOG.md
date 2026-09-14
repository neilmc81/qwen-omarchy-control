# INSTALL_LOG - what was changed on this machine

Rebuilt on **2026-09-13** as a full replacement for the previous `omarchy-voice`
(OpenRouter whisper→planner→TTS) system, which was moved to a backup.

## Audit snapshot (Phase 0)

| Item | Value |
| --- | --- |
| Omarchy | 4.0.3-1 (Arch `ID=omarchy`, kernel 7.2.3-arch1-3) |
| Hyprland | 0.56.2 (Lua dispatcher API, `hl.dsp.*`) |
| Node / npm | v26.8.1 / 11.19.0 |
| Python | 3.14.7 |
| Git | 2.55.0 |
| PipeWire | 1.6.8, wireplumber running, no pulseaudio daemon |
| Audio | Sink 56 `Built-in Audio Analog Stereo`; source 57 (analog stereo) |
| User | `neil`, Wayland session `wayland-1` |
| Existing AI coders | hermes (native ACP), opencode (native ACP), codex (adapter), claude, gemini |
| Existing services | hermes-gateway (enabled), omarchy-voice-wake (disabled), voxtype (enabled) |

## Packages / global installs

- `npm install -g qwen-audio-agent` -> v1.11.0 (159 packages).
- Existing system packages left as-is (portaudio was already installed; the Qwen
  TUI's Python audio bridge pulls `sounddevice` on first Linux use).

## Qwen Audio Agent configuration

- Created `~/.config/qwaudio/config.env` (mode 0600) with:
  - `QWEN_AUDIO_REALTIME_MODEL=qwen-audio-3.0-realtime-flash`
  - `QWEN_AUDIO_REALTIME_BASE_URL=wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime`
    (international / Singapore region)
  - `AGENT_PROTOCOL=hermes`
  - `QWEN_AUDIO_AGENT_BACKEND_PERMISSION_MODE=native`
  - `QWEN_AUDIO_FRONTEND_MCP_CONFIG=.../qwen-omarchy-control/frontend-mcp.json`
  - `DASHSCOPE_API_KEY=` placeholder (user must paste the real key)
- Template backup: `~/.config/qwaudio/config.env.bak-*`
- Persona / desktop-routing instructions added to `~/.config/qwaudio/ASSISTANT.md`

## Gateway user service

- `qwenaudio gateway install` created `~/.config/systemd/user/qwen-audio-agent-gateway.service`.
- Fixed a generated-unit bug: `WorkingDirectory="..."` wrapped in literal quotes
  -> systemd reported "path is not absolute"; removed the quotes.
- Service binds `127.0.0.1:3101` (loopback, not exposed) and reuses Hermes
  authentication. Verified it starts and connects Hermes with a placeholder key
  (to prove wiring; no real credential is used or stored yet).

## Desktop controller (new local project)

Created `~/.local/share/qwen-omarchy-control/` (git repo, MIT):
- `src/qwen_omarchy_control/`: `desktop.py` (controller), `discovery.py`,
  `policy.py`, `mcp.py` (stdio MCP server), `cli.py`.
- `bin/desktop-control`, `bin/desktop-mcp`, `bin/qwen-voice-toggle.sh`,
  `bin/qwen-logs.sh`.
- `tests/` - 24 unit tests (policy levels, discovery, MCP wire, controller args).
- `frontend-mcp.json` - enables 17 desktop tools through the Qwen frontend MCP
  client (fast path for simple desktop commands).
- `uninstall.sh`, `README.md`, `INSTALL_LOG.md`, `.gitignore`, `LICENSE` (MIT
  with attribution to omarchy-voice / qwen-audio-agent).

All tests pass; the MCP wire was verified against the real
`@modelcontextprotocol/sdk` used by the gateway (list + call tools end to end),
also from a bare environment without Wayland/Hyprland env, proving the gateway
service can drive the desktop controller.

## Old system removal

The previous `omarchy-voice` integration was backed up to
`~/.local/share/qwen-omarchy-control-backups/old-system-20260913-180519`:
- bar plugin `voice.indicator` -> backup
- `~/.config/omarchy-voice/` (incl. its env) -> backup (never committed to git)
- `omarchy-voice-wake.service` unit + wants symlinks -> removed
- `~/.local/bin/omarchy-voice` launcher -> backup
- `~/.local/share/omarchy-voice/` project -> backup
- `SUPER+SHIFT+V` re-bound from `omarchy-voice command toggle` to the new
  Qwen voice toggle script.

## Hyprland binding

- Backup `bindings.lua.bak-qwen-<ts>`.
- `SUPER+SHIFT+V` -> `~/.local/share/qwen-omarchy-control/bin/qwen-voice-toggle.sh`
  (comment-tagged `[qwen-omarchy-control]`), verified via `hyprctl binds -j`.

## Desktop app (Linux)

- Built from upstream source: `qwen-audio-agent-1.11.0-linux-x86_64.AppImage`
  (had to add an `author` to `desktop/package.json` for electron-builder) ->
  `~/Downloads/qwen-desktop/`. AppImage runtime verified (`--appimage-version`);
  GUI first-run deferred until the realtime key is present.

## Significant decisions

- **Backend chosen: Hermes** (native ACP, `hermes acp`), reusing its existing
  openrouter/deepseek model config; its native permission prompts remain active
  (mode `native`). No duplicate coding-agent install.
- **Fast path**: frontend MCP based local desktop tools (an officially supported
  extension point), avoiding a coding-agent round-trip for every small command.
- **Permission mode** `native` everywhere; `full` was not enabled (no strong
  technical reason on this voice-input machine).

## Runtime source install (why the npm package was replaced)

The published `qwen-audio-agent@1.11.0` npm tarball was found to be broken/incomplete:
- The `tui/src/` was missing files (`input-parts.mjs`, ...), so `qwenaudio tui`
  crashed on launch ("Cannot find module tui/src/input-parts.mjs").
- `server/src/frontend/` (the whole frontend-tool/MCP subsystem) was absent, so
  the documented `QWEN_AUDIO_FRONTEND_MCP_CONFIG` fast path was **not available**
  in the published release at all.

Fix: the runtime was installed from the upstream git `main` branch (same 1.11.0
version, complete tree) into the global node_modules path:
- `cp -a` of the cloned source + its `node_modules` (deps were synced from the
  working repo copy; the registry re-fetch of `@agentclientprotocol/sdk@1.4.0`
  and `@modelcontextprotocol/sdk` drops their `dist/` builds, so those were
  restored from the source checkout as well).
- The `qwenaudio` global shim (`$GP/bin/qwenaudio`) is a symlink to
  `cli/bin/qwenaudio.mjs`; the mjs was accidentally overwritten during setup and
  restored from source.

Local patches applied to the installed runtime (all documented, reversible):
1. `server/src/backend/adapters/acp/drivers/local-acp.mjs` — parameterized
   `externalMcp` and set it `false` for the **Hermes** driver. Hermes' ACP
   `initialize` does not advertise `mcpCapabilities.http`, which the gateway's
   coordinator-MCP assertion requires; without this patch the gateway marked the
   backend `START_FAILED` and delegation was disabled. With it, delegation works
   and only gateway-injected (coordinator) tools are skipped for Hermes. This is
   a gateway-side change only; `~/.hermes` is untouched.
2. `tui/src/input-parts.mjs` was copied from source when running from the npm
   package; no longer needed after the source install.

Because the runtime now lives outside npm's registry management, update it with:
```bash
cd /tmp/opencode/qwen-audio-agent && git pull && rsync -a --exclude .git --exclude dist --delete ./ "$(npm prefix -g)/lib/node_modules/qwen-audio-agent/"
# re-apply the hermes externalMcp patch after any update
```

## Measured latency (acceptance run, 2026-09-13)

Automated end-to-end tests through the realtime pipeline (text drive + synthetic
speech via a virtual mic), plus the desktop fast path:

| Path | Measured |
| --- | --- |
| Speech-in -> first transcript -> first reply (via virtual mic, Qwen realtime) | ~113-500 ms |
| Text command -> first assistant reply (simple Q&A) | ~255 ms |
| Desktop command through MCP fast path (`get_audio_status`) | ~255 ms |
| Desktop command (`switch_workspace`) observed | ~0.5-0.8 s |
| Backend delegation (`git status` task via Hermes ACP) | task elapsed 14.3 s |
| Desktop CLI subprocess fast-path (5 read-only ops, 5 runs) | median 240 ms |

No OpenAI realtime/STT/TTS is used anywhere in the voice path.

## Pending user input / not done yet

- DashScope API key (placeholder is in place; see the final report for where to paste).
- First realtime voice call + acceptance tests + latency numbers.
- Wake-word (Desktop app only) first enable.

## Iteration 2026-09-13 (late): confirm gating, mouse control, bar-state fix

- [x] **Confirm/cancel gating** (voice safety): `close_active_window`,
  `move_active_window_to_workspace` and `set_volume` no longer run immediately.
  The MCP server returns a pending action; the assistant asks out loud, then
  `confirm_pending` / `cancel_pending` run or discard it. Read-only tools keep
  working while pending; all other tools are blocked until resolved.
  Verified live: "close this window" -> "Shall I close the active window?" ->
  "yes" -> `confirm_pending` executed.
- [x] **Mouse control** (`pointer_move` / `mouse_click` / `mouse_scroll`):
  pointer via `hl.dsp.cursor.move`; clicks and wheel via `ydotool`. Enabled
  `ydotool.service` (user unit, now enabled + active; socket
  `/run/user/1000/.ydotool_socket`). Verified live: pointer_move + scroll.
- [x] **Bar indicator desync fix**: the icon showed muted while the mic was
  still listening. Root cause: the toggle script tracked its own copy of the
  mute state. The TUI now OWNS `$XDG_RUNTIME_DIR/qwen-voice/state.json`
  (patched `tui/src/index.mjs`, see `patches/patch-tui.py`, 8 replacements,
  idempotent, re-run after upgrades): reads it at start (defaults to muted),
  writes every mute/unmute, self-mute events, and on exit (`stopped`), and
  re-asserts the mute to the gateway when voice ownership becomes active.
  The toggle script only sends `/m` and reads back the real state.
  Verified: fresh start = muted, toggle on/off flips the file, gateway
  restart keeps the TUI muted and the icon consistent.
- Frontend MCP tool count: 21 -> **26**.

## Acceptance results (updated 2026-09-13, key configured)

- [x] 1. Assistant activation (TUI opened by hotkey, mic on, DashScope realtime connected)
- [x] 2. Hears normal speech (virtual-mic speech test -> transcript + reply)
- [x] 3. Answers using Qwen realtime voice (flash model over DashScope)
- [x] 4. No OpenAI realtime/STT/TTS anywhere
- [x] 5. Interruption: half-duplex `/interrupt` / `m` supported by the TUI
- [x] 6. "Open Chrome" (launch_app fast path)
- [x] 7. Terminal route (omarchy launch terminal)
- [x] 8. "What window am I using" (get_active_window fast path)
- [x] 9. "What apps are open" (list_windows fast path, accurate list)
- [x] 10. Switch to workspace (fast path, verified workspace change)
- [x] 11. Move this window to workspace N (fast path, verified both ways)
- [x] 12. Turn the volume down (fast path, verified 100->95%)
- [x] 13. Mute the computer (fast path, verified sink muted)
- [x] 14. Unmute the computer (fast path, verified unmuted)
- [x] 15-18. Coding-agent task (git status of ~/Work) reached Hermes; result returned to voice
- [ ] 19-21. Destructive-command gating: Hermes `approvals.mode: off` in ~/.hermes config — a spoken delete executed without a prompt. **RESOLVED BY USER: leave approvals off (intentional). The voice assistant inherits your coder's no-prompt destructive policy; only voice-safe desktop operations are hard-gated by the controller.**
- [x] 22-25. Restart: config survived, frontend MCP (17 tools) + backend READY, single gateway process, no orphaned MCP processes