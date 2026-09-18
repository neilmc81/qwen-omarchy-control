import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui

// Qwen voice assistant state indicator.
// Green mic when the assistant is listening, dimmed/dark when muted or stopped.
// State is published by bin/qwen-voice-state from the TUI's own visible output
// (the stock package exposes the client mute state nowhere else), refreshed by
// bin/qwen-voice-watch.sh only when it changes. No vendor file is patched.
// Uses the standard BarIconButton so sizing/centering matches the other icons.

BarWidget {
  id: root
  moduleName: "qwen.voice"

  property string status: "stopped"
  property string label: ""

  readonly property var icons: ({
    "listening": "󰍬",
    "muted": "󰍭",
    "stopped": "󰍭"
  })

  readonly property color listenColor: "#46E36B"
  readonly property bool listening: root.status === "listening"
  readonly property string tooltipText: root.label !== ""
    ? root.status + " — " + root.label
    : "Qwen voice: " + root.status

  FileView {
    id: state
    path: Quickshell.env("XDG_RUNTIME_DIR") + "/qwen-voice/state.json"
    watchChanges: true
    onFileChanged: reload()
    onLoaded: {
      try {
        const parsed = JSON.parse(state.text())
        root.status = parsed.status || "muted"
        root.label = parsed.label || ""
      } catch (e) {
        root.status = "stopped"
        root.label = ""
      }
    }
    onLoadFailed: {
      root.status = "stopped"
      root.label = ""
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.icons[root.status] || root.icons["muted"]
    active: root.listening
    activeColor: root.listenColor
    dimmed: !root.listening
    tooltipText: root.tooltipText
    onPressed: function(b) {
      if (b === Qt.LeftButton && root.bar)
        root.bar.run(Quickshell.env("HOME") + "/.local/share/qwen-omarchy-control/bin/qwen-voice-toggle.sh")
    }
  }
}