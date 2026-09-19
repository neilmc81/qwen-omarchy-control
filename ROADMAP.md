# Roadmap: voice + computer use (Jev + Cua)

Ideas backlog for the Qwen voice agent. Nothing here is a commitment or a claim
that it works — each item is a proposal, and anything built must earn its place
by measurement, the same way the triage layer did (log first, enforce later).

Current state when this was written (2026-09-19):

- `find_element` / `click_element` are live (Cua accessibility tree + Jev
  selection, delivered by ydotool). See README → "Precise element targeting".
- `triage` is implemented but **off** (`desktop-control triage set log` to
  measure). See README → "Pre-dispatch agent triage".
- Both are off the realtime voice loop; neither adds speech latency.

## Verified available from cua-driver but not yet used

Checked live on this machine, not from docs:

- **`verify_state`** — deterministic postcondition check. Ran it: `status:
  satisfied`, 2 samples. This is the missing half of a click.
- **`invoke_menu`** — resolve a native application-menu path level by level.
  Works for conventional app menus (LibreOffice/Inkscape style); Nautilus's
  dynamic menus refuse with `menu_path_unavailable`.
- **Typed browser tools** — `browser_prepare` → CDP gives exact DOM clicks and
  typing (`browser_click`, `browser_type`, `browser_navigate`,
  `browser_dialog`, `browser_download`, `browser_set_input_files`). Needs the
  browser to be a recognized browser process.
- **`start_recording` / `replay_trajectory`** — record tool calls once, replay
  them in order.
- **`drag`, `mouse_drag`, `parallel_mouse_drag`** — drag gestures.
- **`clipboard_read` / `clipboard_write`** — exact clipboard values (no OCR).
- **`set_window_frame`** — exact window geometry with readback.

## Done

### 1. Verify-then-retry — DONE (2026-09-19)
`click_element` re-reads the window after a click and reports `verified` /
`verification` / `verification_reason`. Two things came out of building it:

- An "element exists" predicate is NOT usable for verification: the element we
  clicked necessarily existed before the click, so `verify_state` reported
  success unconditionally (measured: it did exactly that for "the Music
  folder"). Verification compares before/after window state instead.
- **Automatic retries default to 0.** A retry is a second click; on a single
  click that becomes a double-click and navigates, and on a button it can submit
  twice. Measured live: clicking a folder selects it, but grid-cell selection is
  not exposed, so verification sees "no change" and a retry would have
  double-clicked. `retries` is now an explicit opt-in for idempotent targets.

### 10. Panic stop — DONE (2026-09-19)
`SUPER + SHIFT + ESCAPE` toggles `$XDG_RUNTIME_DIR/qwen-voice/stop`.
`click_element` checks it before touching the pointer, and between steps.
`find_element` still works while stopped (look, don't act).

**Extended (2026-09-19):** the flag now lives in `panic.py` and is honoured by
*every* input path, not just `click_element` — the OCR path (`pointer_move`,
`mouse_click`, `mouse_scroll`, `type_text`) was previously ungated, so a
multi-step sequence kept the mouse and keyboard live after the panic key. Each
call checks on entry, so a sequence stops at its next step. Read-only tools stay
available. A **Freeze agent mouse + keys** button in the `qwen.cost` panel
toggles the same flag.

### 4. "What can I do here?" — DONE (2026-09-19)
`vision.describe_actions` reads a window's tree and Jev ranks the meaningful
actions (best first); exposed as the read-only `describe_actions` MCP tool
(level 1) and `desktop-control actions --window <pid|title>`. Never clicks.
Verified live against Nautilus. (Chrome/foot/Electron are `degraded` as known.)

### 14. Trajectory audit — DONE (2026-09-19)
Every `click_element` appends one NDJSON record to
`~/.local/state/qwen-omarchy-control/trajectory.jsonl` (goal, app, outcome,
attempts, latency, cost; mode 0600, unknown fields dropped so a credential cannot
leak in). Log-only, never raises into the action path.
`desktop-control audit stats|review`. `unsatisfied` is not counted as failure —
the summary keeps `verifiable_rate` and `success_rate_of_verifiable` separate,
which is what the #1 finding (unexposed selection state) requires.

### 1b. Jev-verified outcome for non-expressible cases — DONE (2026-09-19)
`verify_outcome_jev`: on an `unsatisfied` diff, one yes/no Jev question over the
before/after trees. Only upgrades `unsatisfied` → `satisfied`; any failure keeps
the original verdict. Off by default (`jevOutcome: false`) so the audit log can
measure the branch before it is trusted. Exercised live (returned a real
probability, correctly kept a negative case unsatisfied).

## Tier 1 — remaining

### 1b. Jev-verified outcome for non-expressible cases
The current check is a state diff. When the change is real but subtle (a value
changed inside a field with no label change), ask Jev a Noul over the fresh tree:
"did the intended outcome happen?" Keep it off the hot path.

**DONE (2026-09-19)** — see above.

## Lesson: a tool is not "shipped" until three surfaces agree

`describe_actions` worked from the CLI the day it was built, but asking the voice
assistant "what can I do here?" got a generic answer. The tool was missing from
two of the three places that make a tool reachable, and the second fix revealed
the first fix had gone to the wrong file:

1. **MCP server** (`mcp.py` TOOLS + `policy.py` level) — built.
2. **Frontend allowlist** (`frontend-mcp.json`) — `frontend-mcp-client.mjs` only
   forwards tools enabled here. Missing here = invisible to the model.
3. **Routing rules** (`~/.config/qwaudio/frontend-agent/PROMPT.md`) — the model
   must be told *when* to call it.

Failure modes differ by direction and none is obvious:
- server-only: silent — the model simply never sees the tool.
- allowlist-only: **loud and fatal** — the client throws
  `Enabled Frontend MCP tool is missing` and drops the whole MCP connection.
- routing in the wrong file: **silent and deceptive** — the model's own
  `PROMPT.md` says the `<assistant_profile>` has no authority over "tools,
  routing, permissions, safety, memory, tasks or facts", and `ASSISTANT.md` *is*
  the profile. So tool guidance written there is void. This is why the first
  attempt at a fix appeared correct (the file changed, the gateway reloaded, the
  tool was enabled) yet changed nothing.

The rule: **routing in `PROMPT.md`, persona in `ASSISTANT.md`, never the
reverse.** `tests/test_mcp.py::FrontendAllowlistTest` pins surfaces 1-2;
`share/prompt.example.md` and `share/assistant.example.md` track surface 3.

### And: a long-lived realtime session anchors on its own history

Even after all three surfaces were correct, the *already-running* session kept
answering the old way. Verified live: told explicitly to "use describe_actions",
the session called it and produced the right answer - but the bare phrase "what
can I do here?" still returned the previous generic reply, because the session
had answered that exact phrase generically six times already and the realtime
model conditions on its recent turns. After restarting the TUI (fresh session),
the same bare phrase called `describe_actions`, hit "no accessibility tree" on
Chrome, and fell back to `read_screen` - correct end to end.

So: **a prompt or tool change needs a fresh session to be observable.** Waiting
for a reconnect is not enough - `PROMPT.md` is re-read on every `buildSession`,
but the conversation history that anchors the model is restored into the new
connection. Restart the TUI (`bin/qwen-voice-toggle.sh` after killing the tmux
session) when changing behaviour, not just the gateway.

Also measured: the model calls `describe_actions` reliably only when the focused
window is explicitly the subject. Naming the window ("what can I do in Files?")
is a stronger trigger than the bare phrase.

### 2. Goal-level actions
`do_gui_task("save this file")` that internally snapshots, selects, clicks,
verifies and retries. One tool, reliable outcome, instead of exposing raw
element targeting to the model. **Next up.** The audit log now exists to measure
whether single verified steps are reliable enough to chain.

### 3. Spoken outcome
"Did it work?" → Jev reads the post-action tree and answers in one sentence.

## Tier 2 — voice-native powers

### 4. "What can I do here?"
Read the focused window's elements, Jev summarizes the 3–5 meaningful actions
aloud. Turns "hunt for the button" into "what are my options".

### 5. Record once, replay by name
"Watch me do this once" → `start_recording`; later "do my monthly report" →
`replay_trajectory`. A macro system for desktop chores with no scripting.

### 6. Multi-step in one breath
"Open Documents, make a folder called Taxes, and move the newest PDF there."
Each step is a bounded Jev choice; code sequences them with verification between
steps. Do NOT start this until #1 is solid — unverified multi-step just
multiplies failure.

### 7. Watch-and-narrate
"Watch that window and tell me when the export finishes." Poll the tree for a
condition and speak when it flips. Extends the existing terminal watching to
GUIs.

## Tier 3 — more ambitious

### 8. "Undo that" for GUI actions
Snapshot window geometry and open windows before a sequence; "undo" restores
layout and closes what the agent opened. A safety net for computer use.

### 9. Plan-and-approve mode
"Walk me through it first" — Jev narrates each step, waits for "go" before each
click. For when the user does not yet trust it.

### 10. Focus-lock + panic stop — DONE (see above). Remaining: freeze a
multi-step sequence mid-flight, not just individual clicks.

### 11. Browser without OCR
`browser_prepare` → typed CDP clicks and typing. Chrome is currently an
OCR-fallback app; this makes it exact. Enables "fill this form from my resume",
"download the invoice".

### 12. Form-fill with Jev mapping
Read a form's fields, Jev maps user data to each field, type, then show the user
for approval before submit. HARD RULE: never auto-submit.

### 13. Cross-app transfer
"Take the table from the spreadsheet and put it in the doc" — clipboard, focus,
verify.

## Tier 4 — discipline (what kept the last three attempts alive)

### 14. Trajectory audit + success scoring
Log every GUI action and its verification result; a dashboard of success rate
per app. This is the "measure before trust" rule applied to computer use — the
same discipline that caught the triage false-positives in log mode.

### 15. Failure memory
Remember `(app, label)` pairs that failed and skip them next time.

### 16. Self-improvement loop
Feed successes and failures back into better element descriptions for Jev
(learned aliases for ambiguous labels).

## Suggested build order

1. ~~Verify-then-retry (#1)~~ and ~~panic stop (#10)~~ — **done**.
2. ~~Trajectory audit (#14)~~, ~~"What can I do here?" (#4)~~, ~~Jev-verified
   outcome (#1b)~~, ~~mid-sequence freeze (#10 remainder)~~ — **done**.
3. **Goal-level actions (#2)** — one `do_gui_task` tool. The audit log now
   provides the reliability data this needs before it is built.
4. **Typed browser (#11)** — PARKED. Measured live: `browser_prepare` on the
   user's running Chrome is refused (`browser_requires_setup`; existing-profile
   attachment needs an explicit launch grant, runtime mode is `standard`). An
   isolated driver-owned browser has no logins, so "download the invoice" does
   not work against real accounts. Only useful for public/form pages — revisit
   only if that is wanted. It does NOT remove the OCR fallback as hoped.
5. **Record/replay (#5)** — the one nobody expects from a voice assistant.

Skip #6/#7/#13 until #2 is solid.

## Nautilus crashes during testing — investigated 2026-09-19

Five SIGSEGV coredumps of `nautilus`, all during automated testing. Omarchy
raised a "Process crashed" notification each time.

**Established by controlled experiments:**

- Nautilus does **not** crash when left alone, when resized in a loop, or when
  screenshotted repeatedly (window and desktop capture, 10+ times each).
- It does **not** crash from *pure* GUI interaction: raw `ydotool` double-clicks
  with no accessibility involvement survived repeated runs.
- It **does** crash when folders are opened in **rapid programmatic
  succession**, which is what the test loop did (open, re-read, open again
  within ~2s, several times).
- The crash is always in GTK4's **GSK renderer** (`gsk_renderer_render`), on the
  main loop's redraw. Switching renderer does not help: it reproduced under the
  default (Vulkan, `libvulkan_intel_hasvk.so`) *and* under `GSK_RENDERER=ngl`
  (`libEGL_mesa.so.0`), so it is the GSK/render path in general, not one GPU
  backend.

**Mitigation shipped:** `click_element` now throttles delivered input to at most
one click per `minIntervalMs` (default 1200 ms). An agent should not machine-gun
a GUI regardless, so this is a sane default even if the crash is never fully
explained. The exact sequence above survived with the throttle in place.

**Not fully isolated / for upstream:** whether the root cause is Nautilus, GTK4,
or the AT-SPI bridge being exercised concurrently with a redraw. A stock GTK4
app under rapid programmatic folder navigation is the smallest reproducer to
file upstream. `GDK_SCALE=2` is set by Omarchy's default `monitors.lua` while
this panel reports scale 1 — a mismatch worth mentioning, but **not** shown to
cause the crash (it reproduced with `GDK_SCALE=1` too).

## Other observations from building #1

- Grid-cell `selected` state is not exposed by Nautilus's AT-SPI tree, which is
  why a single click on a folder cannot be verified. This is a real ceiling:
  verify only what the tree actually publishes.
- cua-driver's `kill_app` refuses to terminate a process it did not launch
  (`foreign_process_termination_denied`), so cleanup uses `pkill` during tests.

## Hard constraints (carry forward)

- Never sit inside the realtime voice loop. Everything hangs off the side as a
  tool the frontend may call; nothing may add latency to speech or drop replies.
- Never patch the vendored `qwen-audio-agent` package (an upgrade wipes it, and a
  double-apply once took the gateway down).
- Measure before enforcing: ship dormant or log-only, review the numbers, then
  turn it on.
- Fail closed in any safety path.
- `click_element` moves the real mouse and takes focus. Announce the takeover,
  and prefer narrow, verified actions.

## Known limits measured on this machine

- Cua's own click is broken on Hyprland: native Wayland windows fail with an X11
  `TranslateCoordinates` error, and background/foreground routes report
  `background_unavailable` / `foreground_unavailable` ("production Hyprland
  input plugin is unavailable"). Targeting comes from Cua's AT-SPI reads;
  delivery is ydotool.
- No accessibility tree on terminals (foot), Electron (opencode), browsers
  (Chrome), or SDL/canvas apps (imv) — `degraded`, so those keep using OCR.
- Only elements with accessible names/labels are targetable.
