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

## TODO — review the triage log before enforcing

`triage` is now **in `log` mode** (since 2026-09-21). It evaluates every
`launch_agent` request but always allows, recording what it *would* have done.

**Do this after the voice agent has handled a few dozen real agent requests:**

```bash
desktop-control triage stats       # raw verdict counts + fallback rate
desktop-control triage review -v   # recent decisions with the prompts
```

Then decide:

- **Low `confirm` rate on requests I meant** and **fallback rate ~0** →
  `desktop-control triage set enforce`.
- **Frequent `confirm` on normal work** → tune thresholds in
  `~/.config/qwen-omarchy-control/triage.json` (policy lives there, not in the
  prompts) and keep measuring.

Log: `~/.local/state/qwen-omarchy-control/triage.jsonl` (0600, secrets masked).
Key resolves read-only from `~/.hermes/.env`; if that breaks, `enforce` fails
closed and holds *every* agent request, so re-check before enforcing.

Reminder: `log` mode does **not** record actual damage — it only records its own
would-be verdict. It measures false alarms, not missed danger.

## Verified available from cua-driver but not yet used

Checked live on this machine, not from docs:

- **`verify_state`** — deterministic postcondition check. Ran it: `status:
  satisfied`, 2 samples. This is the missing half of a click.
- **`invoke_menu`** — resolve a native application-menu path level by level.
  Works for conventional app menus (LibreOffice/Inkscape style); Nautilus's
  dynamic menus refuse with `menu_path_unavailable`.
- **Typed browser tools** — DONE and in use: `browser_click`, `browser_type`,
  `browser_navigate` (see #11 below). Still unused: `browser_dialog`,
  `browser_download`, `browser_set_input_files` — downloads write files and
  belong to the coding backend.
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

### 2. Goal-level actions — DONE (2026-09-21)

`browser_search(query)` was the first goal-level action. The general
`do_gui_task(goal, ...)` across arbitrary apps is now built: `task.py` +
the `do_gui_task` MCP tool. Verified live — the loop opened a folder in a real
Nautilus window (see the binding note at the end).

**The design, from two independent references** (arc-cua, and Cua's own
`cua-driver/examples/jev-use`, which builds on the same cua-driver this project
uses). Both converge on the same loop, so we port the *design*, not the code:

```
code observes the window  ->  builds a bounded candidate/action menu
        ->  Jev picks exactly one option (an id it was given)
        ->  code validates it against the current state, executes it
        ->  code re-observes and checks what actually happened
        ->  repeat until done / stuck / out of budget
```

Non-negotiables carried from the references and this project's own rules:

- **Jev only chooses from options code built.** It never invents coordinates,
  element ids, text, or tools. Literal text comes from the caller (`inputs`).
- **A score is not proof the action worked.** Every step re-observes and
  verifies; the model's confidence never substitutes for looking.
- **Freshness guard.** A chosen target is re-checked immediately before the
  action lands; if the UI moved underneath the decision, discard it and look
  again rather than replaying a stale click.
- **Bounded and fail-closed.** A hard `max_actions`; a repeated no-change streak
  ends the run as `blocked`; the panic flag is checked between every step.
- **Terminal states, reported honestly:** `done`, `blocked`, `needs_agent`.
  `done` requires an observable change, not the model's say-so.
- **Never inside the realtime voice loop.** It is a tool the frontend may call.

**This machine's constraints (measured, from the existing primitives):**

- Delivery is **ydotool/wtype**; cua-driver's own click is broken on Hyprland.
- Native clicks only work where an **accessibility tree exists**. foot,
  Electron, Chrome and SDL are `degraded` — for those `do_gui_task` returns
  `needs_agent` and the caller keeps using the browser/OCR tools.
- Steps are **throttled** (the Nautilus GSK crash guard) and each step is an
  extra Jev round-trip, so this is for a handful of steps, not a long march.

**Shipped as `vision.gui_task(...)` + the MCP tool `do_gui_task`**, reusing
`resolve_window` / `candidates_from_tree` / `select` (choice), the fingerprint
diff (`_verify_outcome`), `panic.guard`, and the trajectory audit. The action
menu per step is built from what the tree actually offers:

| Action | Requires | Note |
| --- | --- | --- |
| `click` | a labelled, enabled candidate | the default verb |
| `double_click` | same | for open/activate |
| `type_text` | an `input_key` the caller supplied | text is never invented |
| `press_key` | a safe fixed key list | Enter/Escape/Tab/arrows |
| `scroll` | direction | |
| `done` | — | only accepted if the step verifies |

**Measured lesson (live, 2026-09-21): bind the window once.** The first
implementation re-resolved the target by its title on every step. It worked until
the task changed the window: opening a folder retitles it (`guitask-demo` ->
`alpha`), so the second observe failed with "no window matches" *after* the click
had already succeeded. The loop now resolves the window once and binds its
pid/window_id for the whole task — window *identity* persists, title does not.
A regression test (`VisionBackendBindingTest`) pins this.

Offered actions the model may pick from are exactly those the live tree
supports; anything else is rejected before delivery, and a "done" that changed
nothing is downgraded to `needs_agent` rather than reported as success.

### 3. Spoken outcome
"Did it work?" → Jev reads the post-action tree and answers in one sentence.

### 11. Browser without OCR — DONE (2026-09-19)
Typed browser control is live: `browser_read`, `browser_click`, `browser_type`,
`browser_navigate`, and the goal-level `browser_search`, all through cua-driver's
CDP binding. Chrome is no longer an OCR-only app. Requires Chrome to expose a
DevTools endpoint and cua-driver to hold the `existing-profile` grant; see
README → "Typed browser control". Three findings from live testing: stale page
refs (re-reading between choosing and clicking turns the click into a silent
no-op); role disambiguation ("Search" picked the combobox over the button);
**and the real ceiling - the typed path needs a SINGLE browser window.** With
two, cua binds heuristically (title-only) and refuses every element read, so
the tools report that honestly. Removing this ceiling means talking CDP
directly instead of through cua's window binding; worth doing if multi-window
browser control matters.

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
3. ~~Goal-level actions (#2)~~ — **DONE**: one bounded, verified `do_gui_task`
   tool (`task.py`). Built from the arc-cua + Cua `jev-use` design. Live-tested
   against Nautilus; a real window re-resolve bug was found and fixed (bind the
   window once, not per step).
4. ~~Typed browser (#11)~~ — **DONE**, with a correction: the earlier "parked"
   verdict was wrong. The existing-profile route does work, via a daemon
   startup grant plus a DevTools port on Chrome, and it drives the real
   logged-in profile. See README → "Typed browser control".
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
