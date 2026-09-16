import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui

// Qwen voice cost tracker: one bar icon, a dropdown on click.
// Data comes from the records qwen-cost-update writes into
// $XDG_STATE_HOME/qwen-voice/cost/ (overview.json, daily.json), watched live.
// Clicking toggles the panel and refreshes the numbers.

BarWidget {
  id: root
  moduleName: "qwen.cost"

  readonly property string stateHome: Quickshell.env("XDG_STATE_HOME") || Quickshell.env("HOME") + "/.local/state"
  property var overview: null
  readonly property var billing: overview && overview.billing ? overview.billing : null

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

  // Shape contract for the bar's popup routing (see clock BarWidget).
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  function togglePanel() {
    if (panelLoader.item) panelLoader.item.toggle()
  }

  // Keep the numbers fresh every few minutes even if the panel sits open.
  Timer {
    interval: 5 * 60 * 1000
    running: true
    repeat: true
    onTriggered: root.refreshData()
  }

  function refreshData(force) {
    if (root.bar) root.bar.run(Quickshell.env("HOME")
      + "/.local/share/qwen-omarchy-control/bin/qwen-cost-update" + (force ? " --refresh" : ""))
  }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "Q"
    tooltipText: root.billing && root.billing.source === "aliyun"
      ? "Qwen Voice — usage & billing"
      : "Qwen billing unavailable — click for connection status"
    onPressed: function(b) {
      if (b === Qt.LeftButton) {
        root.refreshData(true)
        root.togglePanel()
      }
    }
  }
}
