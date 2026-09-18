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
  property bool detailsExpanded: false

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(fg, 1.45)
  readonly property color accent: Color.accent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string stateHome: Quickshell.env("XDG_STATE_HOME") || home + "/.local/state"
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

  function money(value) {
    if (value === null || value === undefined || value === "") return "—"
    return (billing && billing.currency && billing.currency !== "USD" ? billing.currency + " " : "$")
      + Number(value).toFixed(2)
  }

  function detailsText() {
    var rec = overview && overview.month ? overview.month : null
    var lines = []
    if (connected) {
      lines.push("Current payable: " + money(billing.modelStudioPayable))
      if (Number(billing.modelStudioRoundDownDiscount || 0) > 0)
        lines.push("Alibaba applies a rounding discount.")
      lines.push("Total cost matches Model Studio's usage overview; the payable amount includes invoice adjustments and tax.")
      lines.push("Billing updates may be delayed. This month's bill is still accumulating.")
    }
    if (rec) lines.push((tokenCapture && tokenCapture.active ? "Local voice usage: " : "Historical token total: ")
      + Number(rec.totalTokens || 0).toLocaleString()
      + " tokens · " + Number(rec.responses || 0) + " responses")
    if (tokenCapture && !tokenCapture.active && tokenCapture.reason)
      lines.push(tokenCapture.reason)
    if (modelNames.length > 1) lines.push("The total covers all Model Studio usage in this account.")
    return lines.join("\n\n")
  }

  function refresh() {
    if (root.bar) root.bar.run(root.home + "/.local/share/qwen-omarchy-control/bin/qwen-cost-update --refresh")
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
        Text {
          text: root.detailsExpanded ? "Details ▾" : "Details ▸"
          font.family: root.fontFamily
          font.pixelSize: 11
          color: root.accent
          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.detailsExpanded = !root.detailsExpanded
          }
        }
        Text {
          visible: root.detailsExpanded
          width: col.width
          text: root.detailsText()
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: 10
          color: root.dim
        }
      }
    }
  }
}
