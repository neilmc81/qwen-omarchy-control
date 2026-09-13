import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

// Qwen voice assistant state indicator.
// Green mic when the assistant is listening, dark when muted or stopped.
// State comes from the JSON file the qwen-voice-toggle.sh script writes on
// every microphone transition (watched live, no polling).

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
  readonly property color idleColor: root.bar ? root.bar.barForeground : "#888888"

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

  readonly property bool listening: root.status === "listening"
  readonly property string tooltipText: root.label !== ""
    ? root.status + " — " + root.label
    : "Qwen voice: " + root.status

  implicitWidth: holder.implicitWidth
  implicitHeight: holder.implicitHeight

  Item {
    id: holder
    anchors.fill: parent
    implicitWidth: icon.implicitWidth
    implicitHeight: icon.implicitHeight

    Text {
      id: icon
      text: root.icons[root.status] || root.icons["muted"]
      textFormat: Text.PlainText
      font.family: root.bar ? root.bar.fontFamily : "monospace"
      font.pixelSize: 13
      color: root.listening ? root.listenColor : root.idleColor
      opacity: root.listening ? 1 : (root.status === "stopped" ? 0.4 : 0.65)
    }

    Behavior on opacity {
      NumberAnimation { duration: 120; easing.type: Easing.OutCubic }
    }

    MouseArea {
      anchors.fill: parent
      cursorShape: Qt.PointingHandCursor
      onEntered: {
        if (root.bar) root.bar.showTooltip(root, root.tooltipText)
      }
      onExited: {
        if (root.bar) root.bar.hideTooltip(root)
      }
      onClicked: function(mouse) {
        if (mouse === Qt.LeftButton && root.bar) {
          root.bar.run(Quickshell.env("HOME") + "/.local/share/qwen-omarchy-control/bin/qwen-voice-toggle.sh")
        }
      }
    }
  }
}