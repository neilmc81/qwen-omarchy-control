import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "qwen.cost"
  ipcTarget: "qwen.cost"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var overview: null
  // Live: is the agent's mouse/keyboard input frozen? (panic flag present)
  property bool frozen: false

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(fg, 1.45)
  readonly property color accent: Color.accent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string stateHome: Quickshell.env("XDG_STATE_HOME") || home + "/.local/state"
  readonly property string runtimeDir: Quickshell.env("XDG_RUNTIME_DIR") || "/run/user/1000"
  readonly property var billing: overview && overview.billing ? overview.billing : null
  readonly property var tokenCapture: overview && overview.tokenCapture ? overview.tokenCapture : null
  readonly property bool connected: billing && billing.source === "aliyun"
  readonly property var modelNames: overview && overview.perModel ? Object.keys(overview.perModel) : []
  readonly property string modelText: modelNames.length ? modelNames.map(function(name) {
    return name === "qwen-audio-3.0-realtime-flash" ? "Qwen Audio 3.0 Realtime Flash" : name
  }).join("\n") : "No recorded model usage"
  readonly property string checkedText: connected ? String(billing.fetchedAt || "").slice(11, 16) : ""
  readonly property string statusText: connected
    ? "Updated " + checkedText + " · " + (billing.stale ? "Refresh failed" : "Connected")
    : "Billing connection unavailable"

  // Things worth knowing without hunting through docs. Keep short and literal:
  // the panel is narrow, and a truncated hint helps nobody.
  readonly property var helperKeys: [
    { keys: "SUPER+SHIFT+V",     what: "Talk / toggle mic" },
    { keys: "SUPER+SHIFT+CTRL+V", what: "Stop the reply" },
    { keys: "SUPER+SHIFT+ESC",   what: "Freeze agent input" }
  ]

  // Voice capabilities. Phrased as what to SAY, because that is the affordance
  // the user actually has. Quick single actions first, then the chore-level
  // powers (multi-step, macros, watching). Keep every line short: the panel is
  // narrow and a truncated hint helps nobody.
  readonly property var helperTips: [
    { say: "\u201cwhat can I do here?\u201d", what: "Name the window's main actions" },
    { say: "\u201cclick the Save button\u201d", what: "Precise, verified click" },
    { say: "\u201copen the Downloads folder\u201d", what: "Short verified GUI task" },
    { say: "\u201cdid it work?\u201d", what: "Checks and speaks the outcome" },
    { say: "\u201csearch for \u2026\u201d", what: "Looks it up in the browser" },
    { say: "\u201cstop\u201d / \u201cnever mind\u201d", what: "Interrupt it mid-task" }
  ]

  // Chore-level powers. These run several steps or run later, so they are the
  // ones worth reminding yourself about.
  readonly property var helperChores: [
    { say: "\u201c\u2026, then \u2026, then \u2026\u201d", what: "Several steps in one breath" },
    { say: "\u201copen X, make a folder Y, and move Z there\u201d", what: "One ordered sequence, stops at the step that fails" },
    { say: "\u201cwatch me do this once\u201d", what: "Records the steps as a named macro" },
    { say: "\u201cdo my monthly report\u201d", what: "Replays a saved macro by name" },
    { say: "\u201ctell me when the export finishes\u201d", what: "Watches a window, speaks when done" }
  ]

  function money(value) {
    if (value === null || value === undefined || value === "") return "—"
    return (billing && billing.currency && billing.currency !== "USD" ? billing.currency + " " : "$")
      + Number(value).toFixed(2)
  }

  function refresh() {
    if (root.bar) root.bar.run(root.home + "/.local/share/qwen-omarchy-control/bin/qwen-cost-update --refresh")
  }

  // Toggle the freeze flag (same script the panic hotkey runs).
  function toggleFreeze() {
    if (root.bar) root.bar.run(root.home + "/.local/share/qwen-omarchy-control/bin/qwen-voice-panic.sh")
  }

  FileView {
    path: root.stateHome + "/qwen-voice/cost/overview.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      try { root.overview = JSON.parse(text()) } catch (e) { root.overview = null }
    }
    onLoadFailed: root.overview = null
  }

  // The freeze flag is a file: present = frozen. Watching it keeps the panel
  // honest even when the hotkey is used while the panel is open.
  FileView {
    path: root.runtimeDir + "/qwen-voice/stop"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.frozen = true
    onLoadFailed: root.frozen = false
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    centerOnBar: false
    contentWidth: panel.fittedContentWidth(320)
    contentHeight: panel.fittedContentHeight(col.implicitHeight)

    Flickable {
      id: scroll
      anchors.fill: parent
      contentWidth: width
      contentHeight: col.implicitHeight
      interactive: contentHeight > height
      clip: true
      boundsBehavior: Flickable.StopAtBounds

      Column {
        id: col
        width: scroll.width
        spacing: 12

        Item {
          width: col.width
          height: 24
          Text {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "Qwen Voice"
            font.family: root.fontFamily
            font.pixelSize: 13
            font.bold: true
            color: root.fg
          }
          Rectangle {
            anchors.right: parent.right
            width: 28
            height: 24
            radius: 4
            color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.12)
            Text {
              anchors.centerIn: parent
              text: "↻"
              font.family: root.fontFamily
              font.pixelSize: 17
              color: root.accent
            }
            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: root.refresh()
            }
          }
        }

        Column {
          width: col.width
          spacing: 5
          Text {
            text: "This month"
            font.family: root.fontFamily
            font.pixelSize: 11
            color: root.dim
          }
          Text {
            text: root.connected ? root.money(root.billing.modelStudioGrossCost) : "—"
            font.family: root.fontFamily
            font.pixelSize: 28
            font.bold: true
            color: root.fg
          }
          Text {
            text: "Total cost"
            font.family: root.fontFamily
            font.pixelSize: 11
            color: root.dim
          }
        }

        Text {
          width: col.width
          text: (root.modelNames.length > 1 ? "Models: " : "Model: ") + root.modelText
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: 11
          color: root.dim
        }
        Text {
          width: col.width
          text: root.statusText
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: 10
          color: root.connected && !root.billing.stale ? root.dim : "tomato"
        }

        // --- Helper -------------------------------------------------------
        Rectangle {
          width: col.width
          height: 1
          color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.10)
        }

        Text {
          text: "Helper"
          font.family: root.fontFamily
          font.pixelSize: 11
          font.bold: true
          color: root.fg
        }

        Column {
          width: col.width
          spacing: 6

          Repeater {
            model: root.helperKeys
            delegate: Row {
              width: col.width
              spacing: 8
              Text {
                width: 132
                text: modelData.keys
                font.family: root.fontFamily
                font.pixelSize: 10
                color: root.accent
              }
              Text {
                text: modelData.what
                font.family: root.fontFamily
                font.pixelSize: 10
                color: root.dim
              }
            }
          }
        }

        // Live freeze control: click to toggle agent input, same as the hotkey.
        Rectangle {
          width: col.width
          height: 30
          radius: 4
          color: root.frozen
            ? Qt.rgba(0.85, 0.25, 0.25, 0.18)
            : Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.10)
          border.width: 1
          border.color: root.frozen
            ? Qt.rgba(0.85, 0.25, 0.25, 0.55)
            : Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.25)

          Text {
            anchors.centerIn: parent
            text: root.frozen ? "Agent input FROZEN — click to resume"
                              : "Freeze agent mouse + keys"
            font.family: root.fontFamily
            font.pixelSize: 10
            font.bold: root.frozen
            color: root.frozen ? "#ff8a8a" : root.fg
          }
          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.toggleFreeze()
          }
        }

        // Voice tips: the capabilities added with the computer-use work.
        Text {
          text: "Try saying"
          font.family: root.fontFamily
          font.pixelSize: 11
          font.bold: true
          color: root.fg
        }

        Column {
          width: col.width
          spacing: 6

          Repeater {
            model: root.helperTips
            delegate: Column {
              width: col.width
              spacing: 0
              Text {
                width: col.width
                text: modelData.say
                wrapMode: Text.WordWrap
                font.family: root.fontFamily
                font.pixelSize: 10
                color: root.accent
              }
              Text {
                width: col.width
                text: modelData.what
                wrapMode: Text.WordWrap
                font.family: root.fontFamily
                font.pixelSize: 9
                color: root.dim
              }
            }
          }
        }

        // Chore-level powers: multi-step, macros, watching. These are the ones
        // easy to forget, so they get their own heading.
        Rectangle {
          width: col.width
          height: 1
          color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.10)
        }

        Text {
          text: "Chores"
          font.family: root.fontFamily
          font.pixelSize: 11
          font.bold: true
          color: root.fg
        }

        Column {
          width: col.width
          spacing: 6

          Repeater {
            model: root.helperChores
            delegate: Column {
              width: col.width
              spacing: 0
              Text {
                width: col.width
                text: modelData.say
                wrapMode: Text.WordWrap
                font.family: root.fontFamily
                font.pixelSize: 10
                color: root.accent
              }
              Text {
                width: col.width
                text: modelData.what
                wrapMode: Text.WordWrap
                font.family: root.fontFamily
                font.pixelSize: 9
                color: root.dim
              }
            }
          }
        }

        Text {
          width: col.width
          text: "Qwen announces before it uses the mouse, and only clicks "
              + "controls it can name. Every action is verified; it reports "
              + "honestly when it cannot confirm the result."
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: 10
          color: root.dim
        }
      }
    }
  }
}
