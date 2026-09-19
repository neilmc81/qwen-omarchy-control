# Upstream bug report: Nautilus SIGSEGV in GTK4 GSK renderer

Ready to file at https://gitlab.gnome.org/GNOME/nautilus/-/issues/new
(GNOME's tracker is GitLab, not GitHub, so this needs a GNOME account.)

## Title

Segfault in `gsk_renderer_render` when folders are opened in rapid succession

## Description

Nautilus 50.3.1 segfaults in GTK4's GSK renderer when folders are opened in
rapid programmatic succession (open a folder, then open another within about
2 seconds, repeated several times). The crash is on the main loop's redraw.
This was hit while driving Nautilus with an automation agent; a human clicking
at human speed does not trigger it.

## Environment

- Nautilus 50.3.1-1
- GTK 4.22.4-1
- Mesa 26.2.2-1, vulkan-intel 26.2.2-1
- Hyprland on Wayland (Omarchy), Intel Broadwell-U GT2 (HD Graphics 5500)
- Display 1366x768, compositor scale 1
- `GDK_SCALE=2` is exported by Omarchy's default `monitors.lua` (noted as a
  possible mismatch; see below)

## Backtrace

Reproduces under **both** the default Vulkan renderer and `GSK_RENDERER=ngl`,
so this is not specific to one GPU backend. The frames below are from the `ngl`
run (EGL); the Vulkan run is identical in shape but through
`libvulkan_intel_hasvk.so` instead of `libEGL_mesa.so.0`.

```
Thread 1 (main):
#0  ?? () from /usr/lib/libwayland-client.so.0
#1  wl_display_dispatch_queue_pending () from /usr/lib/libwayland-client.so.0
#2  ?? () from /usr/lib/libEGL_mesa.so.0
#3  ?? () from /usr/lib/libEGL_mesa.so.0
#4  ?? () from /usr/lib/libEGL_mesa.so.0
#5  ?? () from /usr/lib/libEGL_mesa.so.0
#6  ?? () from /usr/lib/libgtk-4.so.1
#7  ?? () from /usr/lib/libgtk-4.so.1
#8  ?? () from /usr/lib/libgtk-4.so.1
#9  ?? () from /usr/lib/libgtk-4.so.1
#10 gsk_renderer_render () from /usr/lib/libgtk-4.so.1
#11 ?? () from /usr/lib/libgtk-4.so.1
#12 ?? () from /usr/lib/libgtk-4.so.1
#13 ?? () from /usr/lib/libgtk-4.so.1
#14 ?? () from /usr/lib/libgobject-2.0.so.0
#15 g_signal_emit_valist () from /usr/lib/libgobject-2.0.so.0
#16 g_signal_emit () from /usr/lib/libgobject-2.0.so.0
#17 ?? () from /usr/lib/libgtk-4.so.1
#18 ?? () from /usr/lib/libgobject-2.0.so.0
#19 g_signal_emit_valist () from /usr/lib/libgobject-2.0.so.0
#20 g_signal_emit () from /usr/lib/libgobject-2.0.so.0
#21 ?? () from /usr/lib/libgtk-4.so.1
#22 ?? () from /usr/lib/libglib-2.0.so.0
...
#24 g_main_context_iteration () from /usr/lib/libglib-2.0.so.0
#25 g_application_run () from /usr/lib/libgio-2.0.so.0
#26 main () from /usr/bin/nautilus
```

Signal: SIGSEGV (`SEGV_MAPERR`).

## Steps to reproduce

Automated (what actually triggers it):

1. Open Nautilus at `Home` (the icon grid).
2. Focus the window, move the pointer to a folder icon, double-click it.
3. Wait ~2 seconds, then double-click a different folder.
4. Repeat 3–4 times.

Crash occurs around the second or third iteration.

## What does NOT reproduce it

Tested and stable:

- Nautilus left idle.
- Resizing the window in a loop (forces full redraws).
- Repeated window and desktop screenshots (10+ each).
- Pure synthetic double-clicks via `ydotool` with no accessibility (AT-SPI)
  involvement, repeated.
- A single AT-SPI tree walk followed by pure synthetic double-clicks.

So the trigger looks like the combination of rapid folder navigation *and*
accessibility-tree reads being exercised between navigations, rather than
either alone. I could not isolate it further.

## Workaround

Spacing the interactions by ~1.2s between clicks avoids it entirely (this is
what the automation now does). `GSK_RENDERER=ngl` does **not** avoid it.

## Extra note

`GDK_SCALE=2` is exported by Omarchy's default
`/usr/share/omarchy/config/hypr/monitors.lua` while the panel reports
compositor scale 1. This mismatch is probably worth mentioning, but it is **not**
the cause: the crash also reproduces with `GDK_SCALE=1` forced.
