# qwen-omarchy-control

Low-latency, voice-controlled AI assistant on Omarchy (Arch + Hyprland + PipeWire).
Built around **Qwen Audio Agent** (Alibaba/DashScope realtime voice) with **Hermes**
as the delegated coding backend, and a small local **desktop controller** for instant,
safe desktop commands.

Planned work and ideas for the voice + computer-use surface live in
[ROADMAP.md](ROADMAP.md).

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
| qwen-audio-agent | realtime voice runtime (gateway + TUI), **stock/unmodified** | global npm package (`/home/neil/.local/share/mise/installs/node/26.8.1/lib/node_modules/qwen-audio-agent`) |
| Desktop controller | structured, allowlisted operations | `~/.local/share/qwen-omarchy-control/` |
| MCP server | stdio JSON-RPC exposing the controller to the voice frontend | `bin/desktop-mcp` |
| Voice frontend model | `qwen-audio-3.0-realtime-flash` via DashScope | `~/.config/qwaudio/config.env` |
| Coordinator | persistent desktop-action executor (Unix socket) | `omarchy-voice-coordinator.service` |
| Bar widgets | mic state + cost | `~/.config/omarchy/plugins/{qwen.voice,qwen.cost}` |

> **Design note.** The voice loop is deliberately left to Qwen's own code. An
> earlier iteration inserted a "controlled voice bridge" overlay into the
> gateway's turn/commit path to add a status widget and a physical stop key.
> That overlay made the assistant unreliable: it disabled server-side turn
> detection, let audio pile up to the 30-second cap, dropped tool results and
> cancelled in-flight replies, so commands ran but nothing was spoken back.
> It was removed. Everything this repo adds now hangs **off the side** of the
> voice loop (local MCP tools, hotkey helpers, bar widgets) and never inside it.
> Reliability measured after removal: tool calls `received → result_ready →
> playback.started` with zero failures.

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

`SUPER + SHIFT + V` = **push-to-talk, no window**. The stock Qwen TUI runs
hidden in a detached tmux session (`qwen-voice`); each press toggles the
microphone (`/m`), and a desktop notification reports the state. Replies are
spoken through the speakers; nothing pops up on screen.

The stock TUI opens the microphone on start. On first press the toggle script
sends `/m` and then *waits for the TUI to report itself muted*; if it cannot
confirm that, it kills the session rather than leave an open microphone. Nothing
in the npm package is modified.

- To see the TUI: `tmux attach -t qwen-voice` (leave with `Ctrl-b d`).
- To stop the hidden TUI: `tmux kill-session -t qwen-voice`.

Defined in `~/.config/hypr/bindings.lua` under a clearly commented block
`[qwen-omarchy-control]`. Revert by deleting that block.

Backups of configs before edits:
- `~/.config/hypr/bindings.lua.bak-qwen-<timestamp>`
- `~/.config/qwaudio/config.env.bak-*`

## Bar indicator

A mic icon sits in the top bar right after the weather widget
(`~/.config/omarchy/plugins/qwen.voice/`, slot `qwen.voice` in `shell.json`):
- **Green** mic = listening; **dark** mic = muted/stopped.
- Click it to toggle the microphone (same as the hotkey).
- State comes from `$XDG_RUNTIME_DIR/qwen-voice/state.json`, written by
  `bin/qwen-voice-state` from the TUI's own visible output (the stock package
  exposes the client mute state nowhere else). `bin/qwen-voice-watch.sh` polls
  and rewrites only on change, so the bar never shows a stale or invented state
  and **no vendor file is patched**.
- Source of the plugin is mirrored in this repo under `bar-widget/`.
- Remove the `qwen.voice` entry from `~/.config/omarchy/shell.json` and delete
  `~/.config/omarchy/plugins/qwen.voice/` to revert.

## Language and reply length

Set in `~/.config/qwaudio/USER.md` (the gateway's user-preference file):
- replies are always in English (never Chinese unless you ask in Chinese);
- ordinary answers are 1-2 short sentences; lead with the conclusion;
- when unsure, one short "I'm not sure" + at most one brief clarifying question,
  then stop; no rambling or guessing;
- after a desktop action, confirm in one sentence.

These apply to new voice sessions (restart the gateway service to reapply).

## Cost tracking

A **cost/usage dropdown** in the bar (`qwen.cost` slot, `$` icon right after the
mic). Click it (or `omarchy shell shell summon qwen.cost`) to open a panel with
live Alibaba Cloud billing, token usage, and a **Helper** section.

The Helper section is a quick reference plus a live control:

- **Hotkeys** — `SUPER+SHIFT+V` talk/toggle mic, `SUPER+SHIFT+CTRL+V` stop the
  reply, `SUPER+SHIFT+ESC` freeze agent input.
- **Freeze button** — toggles the same flag as the panic hotkey. It reads the
  flag file live, so the button shows "Agent input FROZEN — click to resume"
  whether the freeze came from the button or the key.
- A one-line reminder that Qwen announces when it is about to use the mouse, and
  that saying "stop" interrupts it.

How it works:
- **Live cost is Alibaba Cloud billing** (BSS), fetched by the collector. The
  stock gateway discards `response.done` usage, so token-level figures are only
  available if the gateway journals them itself; the panel labels them
  "historical" when it does not, rather than passing a frozen number off as
  live. This project no longer patches the package to add that journal — the
  patch broke on every upgrade.
- **Collector** `bin/qwen-cost-update` aggregates usage, computes the free-quota
  meter and (optionally) fetches live billing, and writes
  `~/.local/state/qwen-voice/cost/{overview,daily,billing}.json` for the widget
  to watch. Refreshed on widget open, every 5 minutes, and by a systemd timer
  (`qwen-cost-update.timer`, every 30 min).
- **Live billing** (optional): put an Alibaba Cloud AccessKey with `bss:read`
  permission into `~/.config/qwaudio/cost.json` (`accessKeyId` /
  `accessKeySecret`). The collector then calls BSS `QueryAccountBalance` +
  `QueryBillOverview` for the account balance and this month's bill. Without
  credentials it falls back to "local estimate only".
- **Free quota** (optional): set `freeQuota` in the same config —
  `{ "amount": N, "unit": "tokens"|"usd", "label": "..." }`. The meter subtracts
  real consumption and turns red past 90%.
- **Estimated cost** uses per-model `rates` (USD per 1K tokens) in the config;
  fill them in from the DashScope console to see a local cost estimate.

Config lives in `~/.config/qwaudio/cost.json` (0600; contains the AccessKey
secret). Example:

```json
{
  "accessKeyId": "LTAI...",
  "accessKeySecret": "...",
  "freeQuota": { "amount": 1000000, "unit": "tokens", "label": "DashScope free quota" },
  "rates": { "qwen-audio-3.0-realtime-flash": { "inputPer1k": 0.0, "outputPer1k": 0.0 } }
}
```

The widget source is mirrored in this repo under `bar-widget-cost/`.

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
  `unmute_audio`, `get_audio_status`, `get_system_status`,
  `read_window`, `read_screen`, `pointer_move`, `find_element`,
  `describe_actions`
- **Level 2 (careful):** `move_active_window_to_workspace`,
  `close_active_window`, `open_url`, `type_text`, `mouse_click`, `mouse_scroll`,
  `click_element`
- **Confirmation gating:** `close_active_window`, `move_active_window_to_workspace`
  and `set_volume` do NOT run immediately — they return a pending action and the
  assistant asks you to confirm out loud before calling `confirm_pending`
  (or `cancel_pending` to discard). Read-only tools keep working while an action
  is pending; nothing else runs until it is confirmed or cancelled.
- **Mouse control:** `pointer_move` (absolute or relative), `mouse_click`
  (left/right/middle, optional double) and `mouse_scroll` (by screenfuls,
  pointing at the target window first). Pointer movement uses Hyprland's
  `hl.dsp.cursor.move`; clicks and the wheel need the `ydotool` daemon
  (`systemctl --user enable --now ydotool.service`). Every one of these honours
  the panic freeze (see below).

### Three places a tool must be registered

Adding a tool to the MCP server is not enough for the voice model to use it.
There are three surfaces, and a tool missing from any one of them is invisible:

1. **The MCP server** (`src/qwen_omarchy_control/mcp.py`, `TOOLS`) — what the
   server can serve, and `policy.py` for its level.
2. **The frontend allowlist** (`frontend-mcp.json`) — the Qwen frontend only
   forwards tools listed here with `"enabled": true`
   (`frontend-mcp-client.mjs` filters on it). A tool present on the server but
   absent here never reaches the model. **Adding an enabled entry whose name the
   server does not expose is worse: the client throws
   `Enabled Frontend MCP tool is missing` and drops the whole connection.**
   Restart the gateway after editing (`systemctl --user restart
   qwen-audio-agent-gateway.service`).
3. **The routing rules** (`~/.config/qwaudio/frontend-agent/PROMPT.md`) — the
   model must be told *when* to call it. Tool descriptions alone are not enough
   for a conversational trigger like "what can I do here?", which the model will
   otherwise answer from general knowledge ("you can browse the web"). A tracked
   template lives at `share/prompt.example.md`.

**Do not put tool routing in `ASSISTANT.md`.** That file is the
`<assistant_profile>`, and `PROMPT.md` itself declares (lines 16-17) that
anything in the profile "about tools, routing, permissions, safety, memory,
tasks or facts is void". Guidance written there is discarded by the model's own
rules — which is exactly how the first fix failed: `describe_actions` was enabled
and described, and the model still answered generically because the instruction
sat in the profile. Routing belongs in `PROMPT.md`, which is read before the
profile and has real authority. `ASSISTANT.md` keeps persona and style only.

`PROMPT.md` is re-read from disk on every realtime `buildSession` (no caching),
and the session reconnects every few minutes, so an edit takes effect without a
gateway restart.

Level 3 operations (file deletion, package removal, sudo, shutdown, killing
processes, sending messages, entering passwords, arbitrary shell) are **not**
implemented on the voice path. They belong to the coding backend (Hermes),
which keeps its own per-action permission prompts.

## Controlling your applications and agents

- **Coding / build requests now run in a VISIBLE window** (no headless backend):
  say *"build me X"*, *"use hermes to ..."*, *"ask codex to ..."* — the assistant
  calls `launch_agent`, which opens that agent's TUI in a terminal window (foot,
  class `qwen-hermes` / `qwen-codex` / `qwen-opencode`) with your request already
  submitted and the window held open (`foot -H`). You watch it work.
  The headless Hermes ACP backend is disabled (`AGENT_PROTOCOL=` empty).
- **Read-back**: say *"read it back"*, *"what did it say?"* — the assistant OCRs
  the agent window (`read_window`/`read_screen`) and summarizes it out loud.
- **Drive any open window**: `type_text` focuses a window and types (optional
  Enter); aliases: opencode / coding agent / codex / chatgpt / hermes / terminal /
  browser.
- **Find apps**: `launch_app` resolves installed desktop entries; a fix now
  handles desktop files with duplicated keys (e.g. Google Chrome) that a strict
  parser rejected.

Limitations: OCR of terminal/TUI text is approximate (small/low-contrast text
can garble). The realtime model sometimes answers simple "build me X" requests
inline (writes the code itself) instead of opening the agent window — if you
want the visible agent every time, say "use hermes to ..." explicitly. Avoid
literal `/paths` in spoken requests (the Qwen TUI treats absolute paths as file
attachments).

## Precise element targeting (on by default, self-degrading)

The OCR tools guess at pixels: tesseract reads a screenshot and a click lands on
a coordinate. The desktop already exposes a structured accessibility (AT-SPI)
tree, so there is no need to guess. Two extra tools use it:

- **`find_element`** — read-only. Given a goal ("the Documents folder", "the
  Save button") it returns the element's role, label and screen position.
- **`click_element`** — finds that element and clicks it. Add `double: true` to
  open an item.

```
cua-driver get_window_state   ->  candidates (role, label, index, frame)
Jev (System One)              ->  pick exactly one supplied candidate, +confidence
code                          ->  focus window, move pointer to frame, ydotool click
                                  then re-read the window to verify the outcome
```

The model chooses among supplied candidates only — it never invents an element
or a coordinate — and code decides whether the confidence is high enough.

**"What can I do here?"** The same tree powers a read-only `describe_actions`
tool: it reads a window and names the 3–5 meaningful actions in it (best first),
so the user can ask what is available instead of hunting for a button. It never
clicks or moves the mouse and stays available while the panic freeze is set —
looking is always allowed. Try it from the CLI with
`desktop-control actions --window <pid-or-title>`.

**Verify, then report honestly.** After a click the window is re-read and
compared with its state beforehand; the result is returned as `verified`,
`verification` and `verification_reason`. `unsatisfied` means no change was
observable — not necessarily that the click failed (a list selection may not be
exposed in the tree) — but the assistant is told to report what actually
happened rather than assume success.

**When the diff is blind (opt-in).** A state diff cannot see a value that changed
inside an unlabelled field whose label did not. With `jevOutcome: true`, an
`unsatisfied` result triggers one yes/no Jev question over the before/after trees
to catch that subtle success. It only ever upgrades `unsatisfied` → `satisfied`
(never the reverse), any failure degrades to the original verdict, and it is off
by default so it can be measured first — see the audit log below.

**Retries are off by default, deliberately.** A retry is a *second* click: on a
single click that becomes a double-click (which navigates or opens), and on a
button it can submit twice. Measured live: clicking a folder selects it, but
grid-cell selection is not in the tree, so verification correctly saw "no
observable change" — and an automatic retry would have silently double-clicked.
Raise `retries` only for a target known to be idempotent.

**Every action is logged.** Each `click_element` appends one NDJSON record
(goal, app, outcome, attempts, latency, cost) to
`~/.local/state/qwen-omarchy-control/trajectory.jsonl`. This is log-only and
never changes behaviour; it is the "measure before trust" rule applied to GUI
actions. Read it with:

```
desktop-control audit stats     # success rate per app
desktop-control audit review    # recent actions
```

`unsatisfied` is deliberately **not** counted as a failure: a working Nautilus
selection is invisible to the tree, so the summary reports `satisfied`,
`unsatisfied` and `unknown` separately, plus `verifiable_rate` (how often the app
exposes enough state for success to be provable) and
`success_rate_of_verifiable` (of the provable cases, how many succeeded).

**Why the click is delivered by ydotool, not cua-driver.** Measured on this
Hyprland session: cua-driver's accessibility click fails on native Wayland
windows (`X11 error TranslateCoordinates`), and its background/foreground routes
report `background_unavailable` / `foreground_unavailable` ("production
Hyprland input plugin is unavailable"). Its AT-SPI *reads* are reliable and its
element frames are screen-absolute, so targeting comes from Cua and delivery
uses the project's existing input path.

The assistant is instructed to **say out loud** that it is taking control before
a click, and a desktop notification announces it too, because the real mouse
moves and focus is taken.

### Panic stop (freezes the whole input path, mid-sequence)

`SUPER + SHIFT + ESCAPE` toggles a stop flag
(`$XDG_RUNTIME_DIR/qwen-voice/stop`). While it is set, **every** path that
delivers input refuses — not just `click_element`, but the OCR path too
(`pointer_move`, `mouse_click`, `mouse_scroll`, `type_text`). Because each tool
call checks the flag on entry, a multi-step sequence stops at its next step even
though the steps are separate calls. The same key clears it, and a notification
reports which way the toggle went. Read-only tools (`find_element`,
`describe_actions`, `read_screen`) still work while stopped — the agent may look,
it just may not act. The same toggle is available from the `qwen.cost` bar panel
as a **Freeze agent mouse + keys** button.

Enabled by default and needing no config file: the tools self-degrade to a clear
error when cua-driver, the API key, or the window's accessibility tree is
unavailable, and the existing OCR tools remain the path
(`~/.config/qwen-omarchy-control/vision.json`, see `share/vision.example.json`).
Nothing here sits in the realtime voice loop; it is a tool the frontend may
choose to call.

## Pre-dispatch agent triage (dormant, off by default)

The desktop controller is safe by construction (allowlisted argv, no shell,
unknown tools fail closed). The **agent handoff is not**: `launch_agent` submits
a spoken prompt to a coding agent whose approval policy may be `off`, so a
misheard or hostile sentence becomes a real coding task with no gate. Triage
guards that one seam.

It is **off by default** and never sits inside the realtime voice loop, so it
cannot add latency to speech or drop a reply. When enabled, one TypeSafe/System
One (**Jev**) call asks three atomic questions over the request text — what the
request really is (agent task / desktop command / question / garbage), whether
it is clear, and whether it could be destructive — and **code** decides:

| Mode | Behaviour |
| --- | --- |
| `off` (default) | No network call. `launch_agent` behaves exactly as before. |
| `log` | Evaluates and records what it *would* do, but always allows. |
| `enforce` | Refuses mis-heard fragments; holds destructive or unclear agent requests for spoken confirmation. |

Opening an agent **without** a prompt ("open hermes") sends nothing and is never
triaged — only a prompt that submits work is.

```bash
desktop-control triage status          # current mode + policy
desktop-control triage set log         # measure first, change nothing
desktop-control triage review -v       # recent decisions with prompts
desktop-control triage stats           # verdict counts + fallback rate
desktop-control triage set enforce     # only once the numbers look right
desktop-control triage set off
```

`enforce` fails closed: no key, network error, timeout or malformed answer holds
the request for confirmation rather than allowing it. Every decision is appended
to `~/.local/state/qwen-omarchy-control/triage.jsonl` (0600, secrets masked) so
the false-block rate can be measured before enforcement is trusted.

Config: `~/.config/qwen-omarchy-control/triage.json` (0600); see
`share/triage.example.json`. Policy thresholds live in the config, not in the
prompts. The API key is read from `OPENROUTER_API_KEY` or, read-only, from
`~/.hermes/.env`; it is never written to the audit log.

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

Automated acceptance run, 2026-09-13 (see INSTALL_LOG.md for the full table):

| Path | Perceived latency |
| --- | --- |
| Simple voice answer (realtime STT + reply) | ~113-500 ms to first reply |
| Desktop command via MCP fast path | ~255-800 ms |
| Complex task via Hermes delegation | task elapsed ~14 s (plus a ~2 s "starting" ack) |

## Safety note (read this)

Hermes (the delegated coding backend) is configured with `approvals.mode: off`
in `~/.hermes/config.yaml`. **This is your existing coder's policy and you chose
to keep it.** It does not prompt before destructive actions, and the voice layer
inherits that: a spoken "delete the file ..." will be executed by Hermes without
a prompt. If you ever want voice gating, set `approvals.mode: manual|smart` in
that file (Hermes then requests permission, and the gateway surfaces it as a
confirmation you must answer). The desktop *controller* layer (workspaces,
windows, volume, apps, URLs) remains strictly allowlisted regardless.

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