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

  readonly property string runtimeDir: Quickshell.env("XDG_RUNTIME_DIR")

  FileView {
    id: state
    path: root.runtimeDir + "/qwen-voice/state.json"
    watchChanges: true
    printErrors: false
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

  // FileView cannot watch a file (or its parent directory) that does not exist
  // when the shell starts: it reads once, fails, and never recovers, so the
  // indicator stayed dark for the whole session. The state file only appears
  // when the voice TUI first starts, which is usually after the shell. The
  // runtime directory always exists, so watch it until the state file has been
  // read (first the qwen-voice directory appears, then the file inside it),
  // then disarm so the state view's own watch takes over.
  FileView {
    id: stateDirWatch
    path: state.loaded ? "" : root.runtimeDir
    watchChanges: true
    printErrors: false
    onFileChanged: state.reload()
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