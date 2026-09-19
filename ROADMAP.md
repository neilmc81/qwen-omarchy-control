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

## Tier 1 — fix the biggest gap first

### 1. Verify-then-retry
`click_element` currently reports "clicked", not "it worked". After an action,
check the result with `verify_state` (or a Jev Noul over the fresh tree):
satisfied → done; not → retry once → report honestly. This is the difference
between "I clicked Save" and "it saved".

### 2. Goal-level actions
`do_gui_task("save this file")` that internally snapshots, selects, clicks,
verifies and retries. One tool, reliable outcome, instead of exposing raw
element targeting to the model.

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

### 10. Focus-lock + panic stop
A hotkey that freezes the agent mid-sequence and returns the mouse. Extend
`bin/qwen-voice-stop.sh` to computer use. Worth having before anything
ambitious, because the takeover is real (the agent moves the actual cursor).

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

1. **Verify-then-retry (#1)** — small, upgrades the feature already in use.
2. **Panic stop (#10)** — because the takeover is real.
3. **"What can I do here?" (#4)** — highest delight per line of code.
4. **Record/replay (#5)** — the one nobody expects from a voice assistant.
5. **Typed browser (#11)** — removes the biggest remaining OCR fallback.

Skip #6/#7/#13 until #1 is solid.

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
