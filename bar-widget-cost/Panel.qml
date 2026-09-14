import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Dropdown for Qwen voice cost. Reads the records qwen-cost-update writes and
// shows usage, estimated cost, free quota and live Alibaba Cloud billing.

Panel {
  id: root
  moduleName: "qwen.cost"
  ipcTarget: "qwen.cost"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(fg, 1.45)
  readonly property color accent: Color.accent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string stateHome: Quickshell.env("XDG_STATE_HOME") || home + "/.local/state"
  readonly property string overviewPath: stateHome + "/qwen-voice/cost/overview.json"

  property var overview: null

  // Guarded views of the record so no binding dereferences a null overview.
  readonly property var todayRec: root.overview ? root.overview.today : null
  readonly property var monthRec: root.overview ? root.overview.month : null
  readonly property var allRec: root.overview ? root.overview.all : null
  readonly property var quota: root.overview && root.overview.quota ? root.overview.quota : null
  readonly property var billing: root.overview && root.overview.billing ? root.overview.billing : null
  readonly property string updatedText: root.overview ? String(root.overview.updatedAt || "").slice(11, 19) : ""
  readonly property real quotaPercent: root.quota ? Math.max(0, Math.min(100, Number(root.quota.percent || 0))) : 0
  readonly property real quotaAmount: root.quota ? Number(root.quota.amount || 0) : 0
  readonly property real quotaConsumed: root.quota ? Number(root.quota.consumed || 0) : 0
  readonly property bool quotaAlarming: root.quota && root.quotaPercent >= 90

  function refresh() {
    if (root.bar) root.bar.run(Quickshell.env("HOME")
      + "/.local/share/qwen-omarchy-control/bin/qwen-cost-update --refresh")
  }

  function usageLine(rec) {
    if (!rec) return "—"
    return fmtInt(rec.totalTokens) + " tok · " + fmtInt(rec.inputTokens) + " in / "
      + fmtInt(rec.outputTokens) + " out · " + fmtUsd(rec.estCostUsd)
  }

  function fmtInt(v) {
    var n = Number(v || 0)
    if (n >= 1000000) return (n / 1000000).toFixed(2) + "M"
    if (n >= 1000) return (n / 1000).toFixed(1) + "K"
    return String(n)
  }

  function fmtUsd(v) {
    var n = Number(v || 0)
    if (n === 0) return "$0.00"
    if (n < 0.01) return "$" + n.toFixed(4)
    return "$" + n.toFixed(2)
  }

  function billingText() {
    if (!root.billing) return ""
    if (root.billing.source === "aliyun")
      return "Account balance: " + fmtUsd(root.billing.balance)
        + (root.billing.monthBill !== null && root.billing.monthBill !== undefined
          ? " · this month: " + fmtUsd(root.billing.monthBill) : "")
    return "Local estimate only — add an Alibaba Cloud AccessKey to ~/.config/qwaudio/cost.json"
  }

  // ------------------------------------------------------------- data
  FileView {
    path: root.overviewPath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.parse(text())
    onLoadFailed: root.overview = null
  }

  function parse(content) {
    try {
      root.overview = JSON.parse(String(content || ""))
    } catch (e) {
      console.warn("qwen.cost", "bad overview", e)
      root.overview = null
    }
  }

  // ------------------------------------------------------------- popup
  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    centerOnBar: true
    contentWidth: Math.max(260, Math.min(360, panel.availableCardWidth))
    contentHeight: Math.max(120, Math.min(col.implicitHeight + 12, panel.availableCardHeight))

    Flickable {
      id: scroll
      anchors.fill: parent
      contentWidth: width
      contentHeight: col.implicitHeight
      clip: true
      boundsBehavior: Flickable.StopAtBounds

      Column {
        id: col
        width: scroll.width
        spacing: 10

        // ---- header
        Row {
          width: parent.width
          spacing: 8
          Text {
            text: "Qwen Voice Cost"
            font.family: root.fontFamily
            font.pixelSize: 13
            font.bold: true
            color: root.fg
          }
          Item { width: 120; height: 1 }
          Text {
            text: root.updatedText
            font.family: root.fontFamily
            font.pixelSize: 10
            color: root.dim
            verticalAlignment: Text.AlignVCenter
          }
        }

        // ---- usage section
        Item {
          width: col.width
          height: 34
          Text {
            text: "Today"
            font.family: root.fontFamily
            font.pixelSize: 12
            color: root.fg
            anchors.verticalCenter: parent.verticalCenter
          }
          Text {
            text: root.usageLine(root.todayRec)
            font.family: root.fontFamily
            font.pixelSize: 11
            color: root.dim
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
          }
        }
        Item {
          width: col.width
          height: 34
          Text {
            text: "This month"
            font.family: root.fontFamily
            font.pixelSize: 12
            color: root.fg
            anchors.verticalCenter: parent.verticalCenter
          }
          Text {
            text: root.usageLine(root.monthRec)
            font.family: root.fontFamily
            font.pixelSize: 11
            color: root.dim
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
          }
        }
        Item {
          width: col.width
          height: 34
          Text {
            text: "All-time"
            font.family: root.fontFamily
            font.pixelSize: 12
            color: root.fg
            anchors.verticalCenter: parent.verticalCenter
          }
          Text {
            text: root.usageLine(root.allRec)
            font.family: root.fontFamily
            font.pixelSize: 11
            color: root.dim
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
          }
        }

        // ---- free quota
        Rectangle {
          width: col.width
          height: 2
          color: Qt.rgba(root.dim.r, root.dim.g, root.dim.b, 0.25)
        }
        Item {
          width: col.width
          height: 46
          visible: root.quota !== null
          Column {
            spacing: 6
            Text {
              text: "Free quota"
              font.family: root.fontFamily
              font.pixelSize: 12
              font.bold: true
              color: root.fg
            }
            Rectangle {
              width: col.width
              height: 6
              radius: 3
              color: Qt.rgba(root.dim.r, root.dim.g, root.dim.b, 0.2)
              Rectangle {
                width: parent.width * (root.quotaPercent / 100)
                height: parent.height
                radius: 3
                color: root.quotaAlarming ? "tomato" : root.accent
              }
            }
          }
          Text {
            text: root.quota ? fmtInt(root.quotaConsumed) + " / " + fmtInt(root.quotaAmount)
              + " " + (root.quota.unit === "tokens" ? "tokens" : "") + " used" : ""
            font.family: root.fontFamily
            font.pixelSize: 10
            color: root.dim
            anchors.right: parent.right
            anchors.top: parent.top
          }
        }
        Text {
          visible: root.quota === null
          text: "Free quota not set — add it in ~/.config/qwaudio/cost.json"
          font.family: root.fontFamily
          font.pixelSize: 10
          color: root.dim
          width: col.width
          wrapMode: Text.WordWrap
        }

        // ---- billing
        Rectangle {
          width: col.width
          height: 2
          color: Qt.rgba(root.dim.r, root.dim.g, root.dim.b, 0.25)
        }
        Text {
          text: "Billing"
          font.family: root.fontFamily
          font.pixelSize: 12
          font.bold: true
          color: root.fg
          width: col.width
        }
        Text {
          width: col.width
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: 11
          color: root.dim
          text: root.billingText()
        }

        // ---- refresh / hint
        Item { width: 1; height: 2 }
        Row {
          width: col.width
          spacing: 8
          Rectangle {
            width: refreshLabel.implicitWidth + 16
            height: 22
            radius: 4
            color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.15)
            Text {
              id: refreshLabel
              anchors.centerIn: parent
              text: "Refresh"
              font.family: root.fontFamily
              font.pixelSize: 11
              color: root.accent
            }
            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: root.refresh()
            }
          }
          Text {
            text: "Data: ~/.config/qwaudio/state/usage.jsonl"
            font.family: root.fontFamily
            font.pixelSize: 9
            color: root.dim
            anchors.verticalCenter: parent.verticalCenter
            wrapMode: Text.WordWrap
            width: parent.width - 90
          }
        }
      }
    }
  }
}
