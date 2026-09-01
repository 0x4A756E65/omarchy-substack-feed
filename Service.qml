import QtQuick
import Quickshell
import Quickshell.Io

// Keep all network and XML work outside omarchy-shell. The daemon owns a
// process lock, so plugin rescans cannot create duplicate pollers.
Item {
  id: root

  property var shell: null
  property bool active: true
  property string lastError: ""
  property int restartAttempts: 0
  readonly property string backendPath: Qt.resolvedUrl("substack_backend.py").toString().replace(/^file:\/\//, "")

  function start() {
    if (root.active && !daemon.running) daemon.running = true
  }

  function focusedPanel() {
    if (!root.shell || !root.shell.bar || typeof root.shell.bar.findPanelWidget !== "function") return null
    return root.shell.bar.findPanelWidget("0x4a756e65.omarchy-substack")
  }

  function openSettings() {
    var panel = focusedPanel()
    if (!panel) return false
    panel.open()
    panel.showSettings(true)
    return true
  }

  Process {
    id: daemon
    command: ["python3", root.backendPath, "daemon"]

    onStarted: stabilityTimer.restart()

    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var message = String(text || "").trim()
        if (message !== "") root.lastError = message.substring(0, 240)
      }
    }

    onExited: function(exitCode) {
      if (!root.active) return
      stabilityTimer.stop()
      if (exitCode !== 0) root.lastError = "Substack feed service exited with code " + exitCode
      root.restartAttempts += 1
      restartTimer.interval = Math.min(60000, 4000 * Math.pow(2, Math.min(root.restartAttempts - 1, 4)))
      restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    interval: 4000
    repeat: false
    onTriggered: root.start()
  }

  Timer {
    id: stabilityTimer
    interval: 60000
    repeat: false
    onTriggered: {
      root.restartAttempts = 0
      root.lastError = ""
    }
  }

  // Services are singletons, unlike the one bar-widget instance created per
  // monitor. Owning IPC here prevents duplicate target registration while the
  // shell routes panel actions to the focused monitor.
  IpcHandler {
    target: "0x4a756e65.omarchy-substack"

    function open(): string { return root.shell && root.shell.summon("0x4a756e65.omarchy-substack", "") ? "ok" : "unavailable" }
    function close(): string { return root.shell && root.shell.hide("0x4a756e65.omarchy-substack") ? "ok" : "unavailable" }
    function show(): string { return open() }
    function hide(): string { return close() }
    function toggle(): string { return root.shell && root.shell.toggle("0x4a756e65.omarchy-substack", "") ? "ok" : "unavailable" }
    function refresh(): string {
      Quickshell.execDetached(["python3", root.backendPath, "refresh"])
      return "ok"
    }
    function markAllRead(): string {
      Quickshell.execDetached(["python3", root.backendPath, "mark-all-read"])
      return "ok"
    }
    function settings(): string { return root.openSettings() ? "ok" : "unavailable" }
    function toggleHeadline(): string {
      var panel = root.focusedPanel()
      if (!panel) return "unavailable"
      panel.persistSettings({ showTicker: !panel.showTicker })
      return "ok"
    }
  }

  Component.onCompleted: start()
  Component.onDestruction: {
    active = false
    restartTimer.stop()
    stabilityTimer.stop()
    daemon.running = false
  }
}
