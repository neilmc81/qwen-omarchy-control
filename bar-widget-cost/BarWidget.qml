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

  function refreshData() {
    if (root.bar) root.bar.run(Quickshell.env("HOME")
      + "/.local/share/qwen-omarchy-control/bin/qwen-cost-update")
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
    text: ""
    tooltipText: "Qwen voice cost — click for usage & billing"
    onPressed: function(b) {
      if (b === Qt.LeftButton) {
        root.refreshData()
        root.togglePanel()
      }
    }
  }
}
