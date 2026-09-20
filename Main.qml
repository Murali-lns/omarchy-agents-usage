import QtQuick
import Quickshell
import Quickshell.Io

// The display side of agent usage. Omarchy's updater remains authoritative
// for packaged collectors; this plugin runs the Hermes collector, a Grok
// collector that defers to Omarchy when `omarchy-agent-usage-grok` exists,
// and the supplemental model-history phase. This file discovers standard
// records through a bounded listing helper, watches them, and merges optional
// snapshots synced from other machines, bounded by hard file-size, file-count,
// and entry-type limits. Every spawned command runs under a supervisor that
// owns a dedicated process group, caps both streams at the pipe boundary,
// and carries a hard deadline with TERM-then-KILL group reaping.
Item {
  id: root
  visible: false

  property var settings: ({})

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string usageDir: (Quickshell.env("XDG_STATE_HOME") || home + "/.local/state") + "/omarchy/agents/usage"
  // This hidden file is supplemental historical model data, never a provider
  // record. Agent discovery explicitly excludes it below.
  readonly property string modelHistoryPath: usageDir + "/.model-history.json"
  property var modelHistoryData: ({})
  property int modelHistoryRevision: 0

  // ------------------------------------------------------------- discovery

  // ------------------------------------------------ process-boundary hardening
  // Every spawned command runs through supervised-run.sh: a dedicated
  // process group per command (PID == PGID == SID, ownership verified from
  // /proc before the command runs), a cleared environment with a minimal
  // explicit one, both streams capped at the pipe boundary before QML can
  // buffer a byte, and a hard deadline whose TERM-then-KILL escalation is
  // closed out by the bounded group janitor. A hung or flooding helper can
  // neither stall nor exhaust the long-lived shell.
  readonly property string binBash: "/usr/bin/bash"
  readonly property string binMkdir: "/usr/bin/mkdir"
  readonly property string binPython3: "/usr/bin/python3"
  readonly property string omarchyUsageUpdatePath: "/usr/share/omarchy/bin/omarchy-agent-usage-update"

  // Every spawned process resolves its own interpreter the same trusted way
  // (`/usr/bin/bash <script>` or `/usr/bin/python3 <script>`), so shebang
  // lines are never consulted and nothing referenced here resolves via PATH.
  readonly property var minimalChildEnv: ({ "HOME": root.home })

  readonly property int collectorDeadlineMs: 120000
  readonly property int discoveryDeadlineMs: 20000
  readonly property int syncDeadlineMs: 30000
  // Producer-side per-stream cap handed to supervised-run.sh; a child that
  // overruns it is severed at the pipe instead of buffered into the shell.
  readonly property int streamCapBytes: 262144
  // The scan helper's own budget (2 MiB of payload plus markers) sits below
  // this ceiling; the cap is pure defence in depth at the pipe boundary.
  readonly property int syncScanStdoutCapBytes: 2621440
  // Console text is trimmed to this before console.warn. Display hygiene
  // only — the safety cap is producer-side, not a post-hoc truncation.
  readonly property int consoleMessageCapChars: 400

  Timer {
    id: listDeadlineTimer
    interval: root.discoveryDeadlineMs
    repeat: false
    onTriggered: root.killLeaked(listProcess, listExitGuard)
  }
  Timer {
    id: updateDeadlineTimer
    interval: root.collectorDeadlineMs
    repeat: false
    onTriggered: root.killLeaked(updateProcess, updateExitGuard)
  }
  Timer {
    id: hermesDeadlineTimer
    interval: root.collectorDeadlineMs
    repeat: false
    onTriggered: root.killLeaked(hermesProcess, hermesExitGuard)
  }
  Timer {
    id: grokDeadlineTimer
    interval: root.collectorDeadlineMs
    repeat: false
    onTriggered: root.killLeaked(grokProcess, grokExitGuard)
  }
  Timer {
    id: modelHistoryDeadlineTimer
    interval: root.collectorDeadlineMs
    repeat: false
    onTriggered: root.killLeaked(modelHistoryProcess, modelHistoryExitGuard)
  }
  Timer {
    id: syncMkdirDeadlineTimer
    interval: root.syncDeadlineMs
    repeat: false
    onTriggered: root.killLeaked(syncMkdirProcess, syncMkdirExitGuard)
  }
  Timer {
    id: syncScanDeadlineTimer
    interval: root.syncDeadlineMs
    repeat: false
    onTriggered: root.killLeaked(syncScanProcess, syncScanExitGuard)
  }

  // Process objects expose a signal(int) that is delivered to the direct
  // child only. Via supervised-run.sh the direct child leads a dedicated
  // process group (PID == PGID == SID, ownership re-verified from /proc
  // before the command runs), so -PID names exactly that command's group;
  // after TERM the bounded reaper validates ownership again and closes the
  // group out with a final KILL. SIGTERM (15), then SIGKILL (9) — numeric so
  // no headers are needed.
  readonly property int sigTerm: 15
  readonly property int sigKill: 9

  // Runs every spawned command: owns its process group and caps both streams
  // at the pipe boundary before they reach this long-lived process.
  readonly property string supervisedRunScriptPath: {
    var resolved = Qt.resolvedUrl("supervised-run.sh").toString().replace(/^file:\/\//, "")
    if (resolved && resolved.indexOf("/") !== -1) {
      return resolved
    }
    return home + "/.config/omarchy/plugins/io.github.murali-lns.agents-usage/supervised-run.sh"
  }

  // Discovers standard records with producer-side file-count and name bounds
  // instead of an unbounded directory walk.
  readonly property string listRecordsScriptPath: {
    var resolved = Qt.resolvedUrl("list-records.sh").toString().replace(/^file:\/\//, "")
    if (resolved && resolved.indexOf("/") !== -1) {
      return resolved
    }
    return home + "/.config/omarchy/plugins/io.github.murali-lns.agents-usage/list-records.sh"
  }

  // Reaps a terminated child's process group through a bounded, absolute-path
  // runtime helper: ownership validation, TERM to survivors, then KILL.
  readonly property string reaperScriptPath: {
    var resolved = Qt.resolvedUrl("reap-group.sh").toString().replace(/^file:\/\//, "")
    if (resolved && resolved.indexOf("/") !== -1) {
      return resolved
    }
    return home + "/.config/omarchy/plugins/io.github.murali-lns.agents-usage/reap-group.sh"
  }

  // Every spawn goes through the supervisor: absolute interpreter, dedicated
  // process group, and producer-side caps for both streams.
  function supervisedCommand(argv, stdoutCapBytes) {
    var command = [root.binBash, root.supervisedRunScriptPath,
                   String(stdoutCapBytes === undefined ? root.streamCapBytes : stdoutCapBytes),
                   String(root.streamCapBytes)]
    for (var i = 0; i < argv.length; i++) command.push(argv[i])
    return command
  }

  Process {
    id: reaperProcess
    running: false
    command: []
    clearEnvironment: true
    environment: root.minimalChildEnv
    workingDirectory: "/"

    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text) console.warn("agents/reaper", String(text).trim().slice(0, 200))
    }
  }

  function killLeaked(processObject, guard) {
    if (!guard || !guard.active || processObject === null || !processObject.running) return
    guard.active = false
    console.warn("agents", "collector exceeded", processObject.objectName || "deadline", "— terminating")
    try {
      processObject.signal(root.sigTerm)
    } catch (e) {
    }
    // The reaper validates group ownership and closes out any group members
    // that survived TERM, then the guarded onExited path completes normally.
    reaperProcess.command = root.supervisedCommand([root.binBash, root.reaperScriptPath, String(processObject.processId || "")])
    reaperProcess.running = true
  }

  // Guards cooperative exits from being treated as deadline kills. Each
  // process's onExited clears its guard first; a timer that fires on an
  // already-exited process is a no-op because the object is no longer running.
  QtObject {
    id: listExitGuard
    property bool active: false
    onActiveChanged: if (active) listDeadlineTimer.restart()
  }
  Timer {
    id: listExitGuardReset
    interval: 0
    repeat: false
    onTriggered: listExitGuard.active = false
  }
  QtObject {
    id: updateExitGuard
    property bool active: false
    onActiveChanged: if (active) updateDeadlineTimer.restart()
  }
  Timer {
    id: updateExitGuardReset
    interval: 0
    repeat: false
    onTriggered: updateExitGuard.active = false
  }
  QtObject {
    id: hermesExitGuard
    property bool active: false
    onActiveChanged: if (active) hermesDeadlineTimer.restart()
  }
  Timer {
    id: hermesExitGuardReset
    interval: 0
    repeat: false
    onTriggered: hermesExitGuard.active = false
  }
  QtObject {
    id: grokExitGuard
    property bool active: false
    onActiveChanged: if (active) grokDeadlineTimer.restart()
  }
  Timer {
    id: grokExitGuardReset
    interval: 0
    repeat: false
    onTriggered: grokExitGuard.active = false
  }
  QtObject {
    id: modelHistoryExitGuard
    property bool active: false
    onActiveChanged: if (active) modelHistoryDeadlineTimer.restart()
  }
  Timer {
    id: modelHistoryExitGuardReset
    interval: 0
    repeat: false
    onTriggered: modelHistoryExitGuard.active = false
  }
  QtObject {
    id: syncMkdirExitGuard
    property bool active: false
    onActiveChanged: if (active) syncMkdirDeadlineTimer.restart()
  }
  Timer {
    id: syncMkdirExitGuardReset
    interval: 0
    repeat: false
    onTriggered: syncMkdirExitGuard.active = false
  }
  QtObject {
    id: syncScanExitGuard
    property bool active: false
    onActiveChanged: if (active) syncScanDeadlineTimer.restart()
  }
  Timer {
    id: syncScanExitGuardReset
    interval: 0
    repeat: false
    onTriggered: syncScanExitGuard.active = false
  }

  property var agentIds: []
  property var agents: []
  property int dataRevision: 0
  // Compatibility guard for records written before the provider was retired;
  // every other standard record remains generically discoverable.
  readonly property string retiredProviderId: "antigravity"

  function isRetiredProviderId(id) {
    return String(id || "") === root.retiredProviderId
  }

  Process {
    id: listProcess
    running: false
    command: root.supervisedCommand([root.binBash, root.listRecordsScriptPath, root.usageDir])
    clearEnvironment: true
    environment: root.minimalChildEnv
    workingDirectory: "/"

    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyAgentListing(text)
    }
  }

  function rescanAgents() {
    if (!listProcess.running) {
      listExitGuard.active = true
      listProcess.running = true
    }
  }

  function applyAgentListing(output) {
    var ids = []
    var lines = String(output || "").split("\n")
    for (var i = 0; i < lines.length; i++) {
      var name = lines[i].trim()
      if (name === ".model-history.json") continue
      if (name.indexOf("list-meta ") === 0) {
        root.reportListMeta(name.slice(10))
        continue
      }
      if (name.slice(-5) === ".json") {
        var id = name.slice(0, -5)
        if (!root.isRetiredProviderId(id)) ids.push(id)
      }
    }
    ids.sort()
    // Same list, same objects: reassigning the model would tear down every
    // FileView just to build identical ones.
    if (JSON.stringify(ids) !== JSON.stringify(agentIds)) agentIds = ids
  }

  // The bounded listing helper reports its kept/skipped/truncated counts;
  // a capped scan is surfaced instead of being silently shortened.
  function reportListMeta(raw) {
    var meta = null
    try {
      meta = JSON.parse(String(raw || ""))
    } catch (e) {
    }
    if (!meta || typeof meta !== "object") return
    var skipped = Number(meta.skipped) || 0
    var truncated = Number(meta.truncated) === 1
    if (skipped > 0 || truncated)
      console.warn("agents/list", "provider record listing capped: kept "
        + String(Number(meta.kept) || 0) + ", skipped " + String(skipped)
        + (truncated ? ", truncated" : ""))
  }

  Instantiator {
    id: agentInstantiator
    model: root.agentIds

    delegate: Agent {
      required property var modelData
      agentId: modelData
      path: root.usageDir + "/" + modelData + ".json"
      onRecordChanged: root.recordsChanged()
    }

    onObjectAdded: (index, object) => root.rebuildAgents()
    onObjectRemoved: (index, object) => root.rebuildAgents()
  }

  function rebuildAgents() {
    var result = []
    for (var i = 0; i < agentInstantiator.count; i++) {
      var agent = agentInstantiator.objectAt(i)
      if (agent) result.push(agent)
    }
    agents = result
    recordsChanged()
  }

  function recordsChanged() {
    dataRevision++
    scheduleLimitsRetry()
    scheduleSync()
  }

  // A collector that could not reach its limits endpoint at all — typically
  // the seconds after login before the network is up — writes retryAdvised
  // into its record. Honor it with one sooner try instead of waiting out the
  // full refresh interval; a run that reaches the endpoint clears the flag.
  // Only the advising agents rerun, so an outage at one provider does not
  // put every other collector on a 30-second treadmill.
  property var retryAgentIds: []

  Timer {
    id: limitsRetry
    interval: 30000
    repeat: false
    onTriggered: root.runUpdate("limits", root.retryAgentIds)
  }

  function scheduleLimitsRetry() {
    var advising = []
    for (var i = 0; i < agents.length; i++) {
      var record = agents[i] ? agents[i].record : null
      if (record && record.retryAdvised === true && providerEnabled(String(record.id || "")))
        advising.push(String(record.id))
    }
    retryAgentIds = advising
    if (advising.length > 0) limitsRetry.restart()
    else limitsRetry.stop()
  }

  Component.onCompleted: {
    rescanAgents()
    if (syncConfigured()) scheduleSync()
  }

  // -------------------------------------------------------------- refresh

  property int refreshIntervalSec: Math.max(30, Number(setting("refreshIntervalSec", 900)))
  property string pendingUpdateKind: ""

  Timer {
    interval: root.refreshIntervalSec * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.runUpdate("normal")
  }

  Process {
    id: updateProcess
    running: false
    clearEnvironment: true
    environment: root.minimalChildEnv
    workingDirectory: "/"
    onExited: {
      updateExitGuard.active = false
      root.rescanAgents()
      root.checkPendingUpdate()
    }

    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        // Console hygiene only: supervised-run.sh already capped this stream
        // producer-side, so the collector only ever holds bounded text.
        var message = String(text || "")
        if (message.length > root.consoleMessageCapChars) message = message.substring(0, root.consoleMessageCapChars)
        if (message.trim() !== "") console.warn("agents", message.trim())
      }
    }
  }

  readonly property string user: Quickshell.env("USER") || (home ? home.split("/").pop() : "")
  readonly property string hermesCollectorPath: {
    var resolved = Qt.resolvedUrl("hermes-collector.py").toString().replace(/^file:\/\//, "")
    if (resolved && resolved.indexOf("/") !== -1) {
      return resolved
    }
    return home + "/.config/omarchy/plugins/io.github.murali-lns.agents-usage/hermes-collector.py"
  }

  Process {
    id: hermesProcess
    running: false
    clearEnvironment: true
    environment: root.minimalChildEnv
    workingDirectory: "/"
    onExited: {
      hermesExitGuard.active = false
      root.rescanAgents()
      root.checkPendingUpdate()
    }

    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        // Console hygiene only: supervised-run.sh already capped this stream
        // producer-side, so the collector only ever holds bounded text.
        var message = String(text || "")
        if (message.length > root.consoleMessageCapChars) message = message.substring(0, root.consoleMessageCapChars)
        if (message.trim() !== "") console.warn("agents/hermes", message.trim())
      }
    }
  }

  readonly property string grokCollectorPath: {
    var resolved = Qt.resolvedUrl("grok-collector.py").toString().replace(/^file:\/\//, "")
    if (resolved && resolved.indexOf("/") !== -1) {
      return resolved
    }
    return home + "/.config/omarchy/plugins/io.github.murali-lns.agents-usage/grok-collector.py"
  }

  Process {
    id: grokProcess
    running: false
    clearEnvironment: true
    environment: root.minimalChildEnv
    workingDirectory: "/"
    onExited: {
      grokExitGuard.active = false
      root.rescanAgents()
      root.checkPendingUpdate()
    }

    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        // Console hygiene only: supervised-run.sh already capped this stream
        // producer-side, so the collector only ever holds bounded text.
        var message = String(text || "")
        if (message.length > root.consoleMessageCapChars) message = message.substring(0, root.consoleMessageCapChars)
        if (message.trim() !== "") console.warn("agents/grok", message.trim())
      }
    }
  }

  readonly property string modelHistoryCollectorPath: {
    var resolved = Qt.resolvedUrl("model-history-collector.py").toString().replace(/^file:\/\//, "")
    if (resolved && resolved.indexOf("/") !== -1) {
      return resolved
    }
    return home + "/.config/omarchy/plugins/io.github.murali-lns.agents-usage/model-history-collector.py"
  }

  // Model history is supplemental display data. It must run only after every
  // authoritative provider collector has finished, otherwise it can read a
  // half-written provider record and publish a mixed-period sidecar.
  Process {
    id: modelHistoryProcess
    running: false
    clearEnvironment: true
    environment: root.minimalChildEnv
    workingDirectory: "/"
    onExited: {
      modelHistoryExitGuard.active = false
      root.rescanAgents()
      root.checkPendingUpdate()
    }

    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        // Console hygiene only: supervised-run.sh already capped this stream
        // producer-side, so the collector only ever holds bounded text.
        var message = String(text || "")
        if (message.length > root.consoleMessageCapChars) message = message.substring(0, root.consoleMessageCapChars)
        if (message.trim() !== "") console.warn("agents/model-history", message.trim())
      }
    }
  }

  function modelHistoryCommand() {
    return root.supervisedCommand([root.binPython3, root.modelHistoryCollectorPath])
  }

  property bool modelHistoryRequested: false
  property bool primaryLaunchInProgress: false

  function checkPendingUpdate() {
    if (!root.primaryLaunchInProgress && !updateProcess.running && !hermesProcess.running
        && !grokProcess.running && !modelHistoryProcess.running) {
      if (root.modelHistoryRequested) {
        root.modelHistoryRequested = false
        modelHistoryProcess.command = root.modelHistoryCommand()
        modelHistoryExitGuard.active = true
        modelHistoryProcess.running = true
        if (!modelHistoryProcess.running) modelHistoryExitGuard.active = false
        return
      }
      if (root.pendingUpdateKind !== "") {
        var kind = root.pendingUpdateKind
        root.pendingUpdateKind = ""
        root.runUpdate(kind)
      }
    }
  }

  function hermesWanted(agentIds) {
    if (!providerEnabled("hermes")) return false
    if (!agentIds || agentIds.length === 0) return true
    for (var i = 0; i < agentIds.length; i++) {
      if (agentIds[i] === "hermes") return true
    }
    return false
  }

  function grokWanted(agentIds) {
    if (!providerEnabled("grok")) return false
    if (!agentIds || agentIds.length === 0) return true
    for (var i = 0; i < agentIds.length; i++) {
      if (agentIds[i] === "grok") return true
    }
    return false
  }

  function updateWanted(agentIds) {
    if (!agentIds || agentIds.length === 0) return true
    for (var i = 0; i < agentIds.length; i++) {
      if (agentIds[i] !== "hermes" && agentIds[i] !== "grok") return true
    }
    return false
  }

  function hermesCommand(kind) {
    // Interpreter invoked by absolute path; the shebang is never consulted.
    var cmd = [root.binPython3, root.hermesCollectorPath]
    if (kind === "force") cmd.push("--force")
    if (kind === "limits") cmd.push("--limits-only")
    return root.supervisedCommand(cmd)
  }

  function grokCommand(kind) {
    var cmd = [root.binPython3, root.grokCollectorPath]
    if (kind === "force") cmd.push("--force")
    if (kind === "limits") cmd.push("--limits-only")
    return root.supervisedCommand(cmd)
  }

  function updateCommand(kind, agentIds) {
    var command = [root.omarchyUsageUpdatePath]
    if (kind === "force") command.push("--force")
    if (kind === "limits") command.push("--limits-only")
    var providers = settings && settings.providers ? settings.providers : {}
    for (var id in providers) {
      if (root.isRetiredProviderId(id)) continue
      if (providers[id] && providers[id].enabled === false) command.push("--except", id)
    }
    if (agentIds) {
      for (var i = 0; i < agentIds.length; i++) {
        if (!root.isRetiredProviderId(agentIds[i]) && agentIds[i] !== "hermes" && agentIds[i] !== "grok") command.push(agentIds[i])
      }
    }
    return root.supervisedCommand(command)
  }

  function runUpdate(kind, agentIds) {
    if (updateProcess.running || hermesProcess.running || grokProcess.running
        || modelHistoryProcess.running || root.modelHistoryRequested || root.primaryLaunchInProgress) {
      // Collapse queued requests to one full rerun; a forced refresh outranks
      // the cheaper kinds it might have been queued behind.
      if (kind === "force" || root.pendingUpdateKind === "") root.pendingUpdateKind = kind
      return
    }
    // The model-history collector is a second phase of every refresh, even if
    // both primary collectors are disabled for this installation.
    root.modelHistoryRequested = true
    root.primaryLaunchInProgress = true
    if (updateWanted(agentIds)) {
      updateProcess.command = updateCommand(kind, agentIds)
      updateExitGuard.active = true
      updateProcess.running = true
    }
    if (hermesWanted(agentIds)) {
      hermesProcess.command = hermesCommand(kind)
      hermesExitGuard.active = true
      hermesProcess.running = true
    }
    if (grokWanted(agentIds)) {
      grokProcess.command = grokCommand(kind)
      grokExitGuard.active = true
      grokProcess.running = true
    }
    root.primaryLaunchInProgress = false
    Qt.callLater(root.checkPendingUpdate)
    // A launch flag that no process picked up must not leave its guard armed.
    if (!updateProcess.running) updateExitGuard.active = false
    if (!hermesProcess.running) hermesExitGuard.active = false
    if (!grokProcess.running) grokExitGuard.active = false
  }

  function refresh() { refreshAll(true) }
  function refreshAll(force) { runUpdate(force === true ? "force" : "normal") }

  // Opening the panel wants the numbers that go stale on the wire, not
  // another walk over every transcript on disk — the collectors reuse their
  // recent scans in this mode.
  function refreshLimits() { runUpdate("limits") }

  // ------------------------------------------------------------- providers

  // An agent earns a place in the bar and the panel by being switched on in
  // settings and having actually produced numbers — locally or on a synced
  // device. With nothing to show, the whole module collapses out of the bar
  // rather than sitting there dimmed.
  property var enabledProviders: {
    var rev = dataRevision
    var syncRev = syncRevision
    var historyRev = modelHistoryRevision
    var result = []
    var localIds = {}
    for (var i = 0; i < agents.length; i++) {
      var record = agents[i] ? agents[i].record : null
      if (!record || !record.id) continue
      var id = String(record.id)
      if (root.isRetiredProviderId(id)) continue
      localIds[id] = true
      if (!providerEnabled(id)) continue
      var display = displayProvider(record)
      if (providerHasData(display)) result.push(display)
    }
    // An agent that only ever ran on another machine has no local record, but
    // its synced numbers still deserve a tab. Rate limits stay blank — they
    // are per-account and never travel.
    var syncedProviders = syncConfigured() && aggregateData && aggregateData.providers ? aggregateData.providers : {}
    for (var syncedId in syncedProviders) {
      if (localIds[syncedId] || root.isRetiredProviderId(syncedId) || !providerEnabled(syncedId)) continue
      var stats = syncedProviders[syncedId] || {}
      var syncedDisplay = displayProvider({ id: syncedId, name: stats.providerName || friendlyProviderDisplayName(syncedId) })
      if (providerHasData(syncedDisplay)) result.push(syncedDisplay)
    }
    return result
  }

  function providerEnabled(id) {
    if (root.isRetiredProviderId(id)) return false
    if (!settings || !settings.providers || !settings.providers[id]) return true
    return settings.providers[id].enabled !== false
  }

  // All-time keeps a quiet day from hiding an agent; today's counts admit a
  // machine whose only source is history.jsonl, which knows nothing older.
  function providerHasData(p) {
    return !!p && (numberValue(p.totalPrompts) > 0 || numberValue(p.totalSessions) > 0
      || numberValue(p.activeDays) > 0 || numberValue(p.todayPrompts) > 0
      || numberValue(p.todaySessions) > 0 || (p.limits && p.limits.length > 0)
      || !!p.balance || modelUsageByPeriodHasData(p.modelUsageByPeriod))
  }

  // A prepaid agent's credit ledger. Like rate limits, the balance is
  // per-account and never merged across devices.
  function balanceValue(raw) {
    if (!raw || typeof raw !== "object") return null
    var remaining = Number(raw.remaining)
    var funded = Number(raw.funded)
    if (!isFinite(remaining) || remaining < 0) return null
    return {
      remaining: remaining,
      funded: isFinite(funded) && funded > 0 ? funded : 0,
      spent: Math.max(0, Number(raw.spent) || 0),
      currency: String(raw.currency || "USD"),
      estimated: raw.estimated === true
    }
  }

  function friendlyProviderDisplayName(id) {
    var raw = String(id || "")
    var key = raw.toLowerCase()
    if (key === "claude") return "Claude Code"
    if (key === "codex") return "Codex"
    if (key === "fireworks") return "Fireworks"
    if (key === "hermes") return "Hermes"
    if (key === "grok" || key === "xai" || key === "xai-oauth") return "Grok"
    if (key === "openai-codex") return "OpenAI Codex"
    if (key === "opencode-go") return "OpenCode Go"
    if (key === "openrouter") return "OpenRouter"
    if (key === "google" || key === "gemini") return "Google Gemini"
    if (key === "anthropic") return "Anthropic"
    return raw.charAt(0).toUpperCase() + raw.slice(1)
  }

  function displayProvider(record) {
    var providerId = String(record.id)
    var stats = syncedStatsFor(providerId)
    var synced = !!stats
    var deviceCount = synced ? Number(stats.deviceCount || aggregateData.deviceCount || 0) : 0
    var source = synced ? stats : record

    return {
      providerId: providerId,
      providerName: String(record.name || friendlyProviderDisplayName(record.id)),
      ready: record.ready === true || synced,
      usageStatusText: String(record.usageStatusText || ""),
      authHelpText: String(record.authHelpText || ""),

      // Rate limits and balances stay per-account and are never merged
      // across devices.
      limits: Array.isArray(record.limits) ? record.limits : [],
      tierLabel: String(record.tierLabel || ""),
      balance: balanceValue(record.balance),

      todayPrompts: synced ? numberValue(stats.todayPrompts) : numberValue(record.todayPrompts),
      todaySessions: synced ? numberValue(stats.todaySessions) : numberValue(record.todaySessions),
      todayTotalTokens: synced ? numberValue(stats.todayTotalTokens) : numberValue(record.todayTotalTokens),
      todayTokensByModel: synced ? (stats.todayTokensByModel || ({})) : (record.todayTokensByModel || ({})),
      recentDays: synced ? (stats.recentDays || []) : (record.recentDays || []),
      totalPrompts: synced ? numberValue(stats.totalPrompts) : numberValue(record.totalPrompts),
      totalSessions: synced ? numberValue(stats.totalSessions) : numberValue(record.totalSessions),
      activeDays: synced ? numberValue(stats.activeDays) : numberValue(record.activeDays),
      modelUsage: source.modelUsage || ({}),
      // Native record/snapshot data wins for any period it supplies. The
      // sidecar only fills periods missing from older provider records.
      modelUsageByPeriod: mergeModelUsageByPeriod(source.modelUsageByPeriod, modelHistoryPeriodsFor(providerId)),
      hasLocalStats: synced ? (stats.hasLocalStats !== false) : (record.hasLocalStats !== false),
      hasPromptStats: synced ? (stats.hasPromptStats !== false) : (record.hasPromptStats !== false),

      routes: displayRoutes(record.routes, providerId),

      syncEnabled: synced,
      syncDeviceCount: deviceCount,
      syncUpdatedAt: aggregateData && aggregateData.updatedAt ? aggregateData.updatedAt : ""
    }
  }

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  // ------------------------------------------------------------------ sync

  property var syncModeSetting: setting("syncMode", setting("syncEnabled", false))
  property bool syncEnabled: parseSyncEnabled(syncModeSetting)
  property string syncDir: String(setting("syncDir", ""))
  property string syncFileName: String(setting("syncFileName", ""))
  property string syncDeviceId: String(setting("syncDeviceId", ""))
  property string detectedHostname: ""
  readonly property string syncEffectiveDir: expandPath(syncDir)
  readonly property string syncEffectiveFileName: safeSnapshotFileName(syncFileName, syncDeviceId)
  readonly property string syncEffectiveDeviceId: safeDeviceId(syncDeviceId || syncEffectiveFileName.replace(/\.json$/i, ""))
  readonly property string syncSnapshotPath: syncConfigured() ? syncEffectiveDir + "/" + syncEffectiveFileName : home + "/.cache/omarchy/agents-disabled.json"
  property var aggregateData: ({})
  property int syncRevision: 0
  property bool syncRunning: false
  property bool syncRequestedWhileRunning: false
  property string syncStatusText: ""
  property double aggregateUpdatedAtMs: aggregateData && aggregateData.updatedAtMs ? Number(aggregateData.updatedAtMs) : 0

  // The sync-scan.sh helper enforces per-file, aggregate, and file-count
  // limits before synced data reaches this process; the parse-side ceilings
  // below are the defensive backstop for everything downstream of it.
  readonly property string syncScanScriptPath: {
    var resolved = Qt.resolvedUrl("sync-scan.sh").toString().replace(/^file:\/\//, "")
    if (resolved && resolved.indexOf("/") !== -1) {
      return resolved
    }
    return home + "/.config/omarchy/plugins/io.github.murali-lns.agents-usage/sync-scan.sh"
  }
  readonly property int syncMaxScanChars: 2359296 // 2.25 MiB; helper budget plus separator overhead
  readonly property int syncMaxSnapshots: 64
  readonly property int syncMaxProvidersPerSnapshot: 32
  readonly property int syncMaxKeysPerMap: 256
  readonly property int syncMaxKeyLength: 128
  readonly property int syncMaxActiveDates: 3660
  readonly property int syncMaxStringLength: 80
  readonly property double maxCountValue: 1e15

  onSyncEnabledChanged: syncSettingsChanged()
  onSyncDirChanged: syncSettingsChanged()
  onSyncFileNameChanged: if (syncConfigured()) scheduleSync()
  onSyncDeviceIdChanged: if (syncConfigured()) scheduleSync()

  Timer {
    id: syncDebounce
    interval: 1000
    repeat: false
    onTriggered: root.runSync()
  }

  Process {
    id: syncMkdirProcess
    running: false
    clearEnvironment: true
    environment: root.minimalChildEnv
    workingDirectory: "/"
    onRunningChanged: root.updateSyncRunning()
    onExited: function(exitCode) {
      syncMkdirExitGuard.active = false
      if (exitCode !== 0) {
        if (root.syncConfigured()) root.syncStatusText = "Usage sync mkdir failed"
        root.finishSyncRun()
        return
      }
      root.writeSyncSnapshot()
    }
  }

  Process {
    id: syncScanProcess
    running: false
    clearEnvironment: true
    environment: root.minimalChildEnv
    workingDirectory: "/"
    onRunningChanged: root.updateSyncRunning()
    onExited: function(exitCode) {
      syncScanExitGuard.active = false
      if (exitCode !== 0 && root.syncConfigured()) root.syncStatusText = "Usage sync scan failed"
      root.finishSyncRun()
    }

    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.parseSyncScanOutput(text)
    }

    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        // Console hygiene only: supervised-run.sh already capped this stream
        // producer-side, so the collector only ever holds bounded text.
        var message = String(text || "")
        if (message.length > root.consoleMessageCapChars) message = message.substring(0, root.consoleMessageCapChars)
        if (message.trim() !== "") console.warn("agents/sync", message.trim())
      }
    }
  }

  FileView {
    id: syncSnapshotFile
    path: root.syncSnapshotPath
    watchChanges: false
    atomicWrites: true
    printErrors: false
  }

  FileView {
    id: hostnameFile
    path: "/etc/hostname"
    watchChanges: false
    printErrors: false
    onLoaded: root.detectedHostname = String(text() || "").trim()
  }

  FileView {
    id: modelHistoryFile
    path: root.modelHistoryPath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.parseModelHistory(text())
    onLoadFailed: {
      root.modelHistoryData = ({})
      root.modelHistoryRevision++
    }
  }

  function parseModelHistory(content) {
    var parsed = null
    try {
      parsed = JSON.parse(String(content || ""))
    } catch (e) {
      console.warn("agents/model-history", "Ignoring bad model history", e)
    }
    if (!parsed || typeof parsed !== "object" || !parsed.providers || typeof parsed.providers !== "object") {
      root.modelHistoryData = ({})
    } else {
      root.modelHistoryData = parsed
    }
    root.modelHistoryRevision++
  }

  function modelHistoryPeriodsFor(key) {
    var data = root.modelHistoryData
    var providers = data && data.providers && typeof data.providers === "object" ? data.providers : ({})
    var entry = providers[String(key)]
    if (!entry || typeof entry !== "object" || !entry.modelUsageByPeriod
        || typeof entry.modelUsageByPeriod !== "object") return ({})
    return entry.modelUsageByPeriod
  }

  function parseSyncEnabled(value) {
    if (value === true) return true
    var text = String(value || "").trim().toLowerCase()
    return text === "on" || text === "enabled" || text === "true" || text === "yes" || text === "1"
  }

  function syncConfigured() {
    return root.syncEnabled === true && String(root.syncDir || "").trim() !== ""
  }

  function syncSettingsChanged() {
    if (syncConfigured()) {
      scheduleSync()
    } else {
      syncDebounce.stop()
      syncRequestedWhileRunning = false
      aggregateData = ({})
      syncStatusText = ""
      syncRevision++
    }
  }

  function updateSyncRunning() {
    root.syncRunning = syncMkdirProcess.running || syncScanProcess.running
  }

  function scheduleSync() {
    if (!syncConfigured()) return
    syncDebounce.restart()
  }

  function runSync() {
    if (!syncConfigured()) return
    if (root.syncRunning) {
      syncRequestedWhileRunning = true
      return
    }

    syncRequestedWhileRunning = false
    syncStatusText = ""
    syncMkdirProcess.command = root.supervisedCommand([root.binMkdir, "-p", root.syncEffectiveDir])
    syncMkdirExitGuard.active = true
    syncMkdirProcess.running = true
    if (!syncMkdirProcess.running) syncMkdirExitGuard.active = false
  }

  function writeSyncSnapshot() {
    if (!syncConfigured()) {
      finishSyncRun()
      return
    }
    syncSnapshotFile.setText(JSON.stringify(localSnapshot(), null, 2) + "\n")
    Qt.callLater(root.startSyncScan)
  }

  function startSyncScan() {
    if (!syncConfigured()) {
      finishSyncRun()
      return
    }
    // sync-scan.sh reads the folder under hard size, count, and entry-type
    // bounds, and the supervisor relays its output through a cap above the
    // helper's own budget, so StdioCollector below only ever buffers a
    // bounded document.
    syncScanProcess.command = root.supervisedCommand([root.binBash, root.syncScanScriptPath, root.syncEffectiveDir], root.syncScanStdoutCapBytes)
    syncScanExitGuard.active = true
    syncScanProcess.running = true
    if (!syncScanProcess.running) syncScanExitGuard.active = false
  }

  function finishSyncRun() {
    if (syncRequestedWhileRunning && syncConfigured()) {
      syncRequestedWhileRunning = false
      scheduleSync()
    }
  }

  function expandPath(path) {
    var value = String(path || "").trim()
    if (value === "") return ""
    if (value === "~") return home
    if (value.indexOf("~/") === 0) return home + value.substring(1)
    if (value.indexOf("$HOME/") === 0) return home + value.substring(5)
    if (value.charAt(0) !== "/") return home + "/" + value
    return value
  }

  function safeDeviceId(raw) {
    var value = String(raw || "").trim()
    if (value === "") value = Quickshell.env("HOSTNAME") || root.detectedHostname || Quickshell.env("HOST") || Quickshell.env("USER") || "device"
    value = value.replace(/[^A-Za-z0-9_.-]+/g, "-").replace(/^[._-]+|[._-]+$/g, "")
    if (value === "") value = "device"
    return value.length > 80 ? value.substring(0, 80) : value
  }

  function safeSnapshotFileName(rawFileName, rawDeviceId) {
    var value = String(rawFileName || "").trim()
    if (value === "") value = safeDeviceId(rawDeviceId) + ".json"
    value = value.split("/").pop().replace(/[^A-Za-z0-9_.-]+/g, "-").replace(/^[._-]+|[._-]+$/g, "")
    if (value === "") value = safeDeviceId(rawDeviceId) + ".json"
    if (!/\.json$/i.test(value)) value += ".json"
    return value.length > 100 ? value.substring(0, 95) + ".json" : value
  }

  function parseSyncScanOutput(output) {
    var text = String(output || "")
    var scanTruncated = false
    if (text.length > root.syncMaxScanChars) {
      text = text.substring(0, root.syncMaxScanChars)
      scanTruncated = true
    }
    var lines = text.split("\n")
    var snapshots = []
    var currentPath = ""
    var currentJson = []
    var meta = null
    var droppedSnapshots = 0

    function flush() {
      if (currentPath === "") return
      var path = currentPath
      var raw = currentJson.join("\n").trim()
      currentPath = ""
      currentJson = []
      if (path.trim() === "sync-meta") {
        try {
          var parsedMeta = JSON.parse(raw)
          if (isPlainObject(parsedMeta)) meta = parsedMeta
        } catch (e) {
        }
        return
      }
      if (snapshots.length >= root.syncMaxSnapshots) {
        droppedSnapshots++
        return
      }
      try {
        var parsed = JSON.parse(raw)
        if (isPlainObject(parsed) && isPlainObject(parsed.providers)) snapshots.push(parsed)
      } catch (e) {
        console.warn("agents/sync", "Ignoring bad snapshot", String(path).slice(0, root.syncMaxStringLength), String(e).slice(0, 200))
      }
    }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i]
      var start = line.match(/^===(.+)===$/)
      if (start && line !== "=== EOM ===") {
        flush()
        currentPath = start[1]
        currentJson = []
        continue
      }
      if (line === "=== EOM ===") {
        flush()
        continue
      }
      if (currentPath !== "") currentJson.push(line)
    }
    flush()

    aggregateData = aggregateSnapshots(snapshots)
    syncStatusText = syncInputNotice(meta, scanTruncated, droppedSnapshots)
    syncRevision++
  }

  // Skipped or truncated sync input is surfaced in the panel instead of
  // being silently dropped.
  function syncInputNotice(meta, scanTruncated, droppedSnapshots) {
    var parts = []
    var skipped = meta ? numberValue(meta.skipped) : 0
    var truncated = !!meta && Number(meta.truncated) === 1
    if (skipped > 0) parts.push("skipped " + skipped + " oversized or invalid " + (skipped === 1 ? "entry" : "entries"))
    if (truncated || scanTruncated) parts.push("input truncated at safety limits")
    if (droppedSnapshots > 0) parts.push("kept the first " + root.syncMaxSnapshots + " snapshots")
    return parts.length > 0 ? "Usage sync: " + parts.join("; ") : ""
  }

  function cloneValue(value, fallback) {
    if (value === undefined || value === null) return fallback
    try {
      return JSON.parse(JSON.stringify(value))
    } catch (e) {
      return fallback
    }
  }

  function isModelUsagePeriod(id) {
    return id === "today" || id === "7d" || id === "month" || id === "all"
  }

  function isPlainObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value)
  }

  // Period records may carry a numeric sidecar total or a Hermes token bucket.
  // Only token fields count here; quota fields such as percent/remaining never
  // become usage totals.
  function modelUsageValueTotal(value) {
    if (!isPlainObject(value)) return boundedNumber(value)
    var total = 0
    total += boundedNumber(value.inputTokens)
    total += boundedNumber(value.outputTokens)
    total += boundedNumber(value.cacheReadInputTokens)
    total += boundedNumber(value.cacheCreationInputTokens)
    if (total === 0 && value.totalTokens !== undefined)
      total = boundedNumber(value.totalTokens)
    return Math.min(Math.round(total), root.maxCountValue)
  }

  function modelUsageByPeriodHasData(periods) {
    if (!isPlainObject(periods)) return false
    for (var period in periods) {
      if (!isModelUsagePeriod(period) || !isPlainObject(periods[period])) continue
      var values = periods[period]
      for (var modelId in values) {
        if (modelUsageValueTotal(values[modelId]) > 0) return true
      }
    }
    return false
  }

  function cloneModelUsageByPeriod(value) {
    var result = {}
    if (!isPlainObject(value)) return result
    for (var period in value) {
      if (!isModelUsagePeriod(period) || !isPlainObject(value[period])) continue
      result[period] = cloneValue(value[period], ({}))
    }
    return result
  }

  // Native provider/Hermes records are authoritative. A sidecar period is
  // only used when the native record (or an older synced snapshot) lacks it.
  function mergeModelUsageByPeriod(primary, supplemental) {
    var result = cloneModelUsageByPeriod(primary)
    var extra = cloneModelUsageByPeriod(supplemental)
    for (var period in extra) {
      if (result[period] === undefined) result[period] = extra[period]
    }
    return result
  }

  function combineModelUsageValue(additive, current, value) {
    if (isPlainObject(value) && (current === undefined || current === null || isPlainObject(current))) {
      var bucket = isPlainObject(current) ? current : ({})
      combineObjectNumbers(additive, bucket, value)
      return bucket
    }
    if (isPlainObject(current) || isPlainObject(value))
      return combineNumber(additive, modelUsageValueTotal(current), modelUsageValueTotal(value))
    return combineNumber(additive, current, value)
  }

  function combineModelUsageByPeriod(additive, target, source) {
    if (!isPlainObject(source)) return
    for (var period in source) {
      if (!isModelUsagePeriod(period) || !isPlainObject(source[period])) continue
      if (!isPlainObject(target[period])) target[period] = ({})
      var targetPeriod = target[period]
      var sourcePeriod = source[period]
      for (var modelId in sourcePeriod) {
        if (String(modelId).length > root.syncMaxKeyLength) continue
        if (targetPeriod[modelId] === undefined && Object.keys(targetPeriod).length >= root.syncMaxKeysPerMap) continue
        targetPeriod[modelId] = combineModelUsageValue(additive, targetPeriod[modelId], sourcePeriod[modelId])
      }
    }
  }

  function displayRoutes(rawRoutes, providerId) {
    var result = []
    var routes = Array.isArray(rawRoutes) ? rawRoutes : []
    for (var i = 0; i < routes.length; i++) {
      var route = cloneValue(routes[i], null)
      if (!isPlainObject(route)) continue
      var routeKey = String(providerId) + ":" + String(route.id)
      route.modelUsageByPeriod = mergeModelUsageByPeriod(
        route.modelUsageByPeriod,
        modelHistoryPeriodsFor(routeKey)
      )
      result.push(route)
    }
    return result
  }

  // Raw numbers from local records and synced snapshots are clamped to a
  // sane range before they influence totals or display.
  function boundedNumber(raw) {
    var n = Number(raw)
    if (!isFinite(n) || n < 0) return 0
    return n > root.maxCountValue ? root.maxCountValue : n
  }

  function numberValue(value) {
    return Math.round(boundedNumber(value || 0))
  }

  function dateString(date) {
    var y = date.getFullYear()
    var m = String(date.getMonth() + 1).padStart(2, "0")
    var d = String(date.getDate()).padStart(2, "0")
    return y + "-" + m + "-" + d
  }

  function recentDateStrings() {
    var result = []
    for (var offset = 6; offset >= 0; offset--) {
      var date = new Date()
      date.setDate(date.getDate() - offset)
      result.push(dateString(date))
    }
    return result
  }

  function emptyTokenBucket() {
    return { inputTokens: 0, outputTokens: 0, cacheReadInputTokens: 0, cacheCreationInputTokens: 0 }
  }

  // Device-scoped stats add up across machines; account-scoped stats
  // (Fireworks' billing API) are replicas of the same upstream truth on
  // every synced device, so the widest value wins — summing them would
  // double every token per machine.
  function combineNumber(additive, current, value) {
    return additive ? numberValue(current) + numberValue(value) : Math.max(numberValue(current), numberValue(value))
  }

  function combineObjectNumbers(additive, target, source) {
    if (!isPlainObject(source)) return
    var keys = Object.keys(target)
    for (var key in source) {
      if (target[key] === undefined) {
        if (keys.length >= root.syncMaxKeysPerMap || String(key).length > root.syncMaxKeyLength) continue
        keys.push(key)
      }
      target[key] = combineNumber(additive, target[key], source[key])
    }
  }

  function aggregateSnapshots(snapshots) {
    var dates = recentDateStrings()
    var devices = {}
    var providers = {}
    var providerCount = 0

    function providerAcc(id) {
      if (providers[id]) return providers[id]
      if (providerCount >= root.syncMaxProvidersPerSnapshot) return null
      providerCount++
      var recentByDay = {}
      for (var d = 0; d < dates.length; d++) recentByDay[dates[d]] = 0
      providers[id] = {
        providerId: id,
        providerName: "",
        ready: false,
        hasLocalStats: false,
        hasPromptStats: false,
        todayPrompts: 0,
        todaySessions: 0,
        todayTotalTokens: 0,
        todayTokensByModel: ({}),
        recentByDay: recentByDay,
        totalPrompts: 0,
        totalSessions: 0,
        activeDays: 0,
        activeDates: ({}),
        modelUsage: ({}),
        modelUsageByPeriod: ({}),
        devices: ({})
      }
      return providers[id]
    }

    for (var i = 0; i < snapshots.length; i++) {
      var snapshot = snapshots[i]
      var device = safeDeviceId(snapshot.deviceId || "device")
      devices[device] = true
      var snapshotProviders = snapshot.providers || {}
      for (var providerId in snapshotProviders) {
        if (root.isRetiredProviderId(providerId)) continue
        var stats = snapshotProviders[providerId] || {}
        var acc = providerAcc(providerId)
        if (!acc) continue
        acc.devices[device] = true
        if (stats.providerName && acc.providerName === "") acc.providerName = String(stats.providerName).slice(0, root.syncMaxStringLength)
        acc.ready = acc.ready || stats.ready === true
        acc.hasLocalStats = acc.hasLocalStats || stats.hasLocalStats !== false
        // Snapshots from before the field existed only came from agents that
        // count prompts, so a missing value reads as true.
        acc.hasPromptStats = acc.hasPromptStats || stats.hasPromptStats !== false
        var additive = String(stats.scope || "device") !== "account"
        acc.todayPrompts = combineNumber(additive, acc.todayPrompts, stats.todayPrompts)
        acc.todaySessions = combineNumber(additive, acc.todaySessions, stats.todaySessions)
        acc.todayTotalTokens = combineNumber(additive, acc.todayTotalTokens, stats.todayTotalTokens)
        acc.totalPrompts = combineNumber(additive, acc.totalPrompts, stats.totalPrompts)
        acc.totalSessions = combineNumber(additive, acc.totalSessions, stats.totalSessions)
        // Active days overlap between machines, so union the dates rather than
        // summing counts. Snapshots written before activeDates existed only
        // carry a count; the widest one stands in for them.
        var activeDates = Array.isArray(stats.activeDates) ? stats.activeDates : []
        for (var ad = 0; ad < activeDates.length && ad < root.syncMaxActiveDates; ad++)
          acc.activeDates[String(activeDates[ad]).slice(0, 32)] = true
        acc.activeDays = Math.max(acc.activeDays, numberValue(stats.activeDays))
        combineObjectNumbers(additive, acc.todayTokensByModel, stats.todayTokensByModel || {})

        var recent = Array.isArray(stats.recentDays) ? stats.recentDays : []
        for (var r = 0; r < recent.length; r++) {
          var day = recent[r] || {}
          var date = String(day.date || "")
          if (acc.recentByDay[date] !== undefined)
            acc.recentByDay[date] = combineNumber(additive, acc.recentByDay[date], day.messageCount)
        }

        var usage = stats.modelUsage || {}
        for (var modelId in usage) {
          if (String(modelId).length > root.syncMaxKeyLength) continue
          var bucket = acc.modelUsage[modelId]
          if (!bucket) {
            if (Object.keys(acc.modelUsage).length >= root.syncMaxKeysPerMap) continue
            bucket = acc.modelUsage[modelId] = emptyTokenBucket()
          }
          combineObjectNumbers(additive, bucket, usage[modelId] || {})
        }
        combineModelUsageByPeriod(additive, acc.modelUsageByPeriod, stats.modelUsageByPeriod || {})
      }
    }

    var outProviders = {}
    for (var id in providers) {
      var acc = providers[id]
      var recentDays = []
      for (var di = 0; di < dates.length; di++) recentDays.push({ date: dates[di], messageCount: acc.recentByDay[dates[di]] || 0 })
      var providerDevices = Object.keys(acc.devices).sort()
      outProviders[id] = {
        providerId: acc.providerId,
        providerName: acc.providerName,
        ready: acc.ready || providerDevices.length > 0,
        hasLocalStats: acc.hasLocalStats,
        hasPromptStats: acc.hasPromptStats,
        todayPrompts: acc.todayPrompts,
        todaySessions: acc.todaySessions,
        todayTotalTokens: acc.todayTotalTokens,
        todayTokensByModel: acc.todayTokensByModel,
        recentDays: recentDays,
        totalPrompts: acc.totalPrompts,
        totalSessions: acc.totalSessions,
        activeDays: Math.max(acc.activeDays, Object.keys(acc.activeDates).length),
        modelUsage: acc.modelUsage,
        modelUsageByPeriod: acc.modelUsageByPeriod,
        deviceCount: providerDevices.length,
        devices: providerDevices
      }
    }

    return {
      schemaVersion: 1,
      updatedAt: new Date().toISOString(),
      updatedAtMs: Date.now(),
      deviceCount: Object.keys(devices).length,
      devices: Object.keys(devices).sort(),
      providers: outProviders
    }
  }

  // Snapshots keep the field names older Omarchy versions wrote, so a fleet
  // of machines on mixed versions still merges cleanly in both directions.
  function providerSnapshot(record) {
    return {
      providerId: String(record.id),
      providerName: String(record.name || friendlyProviderDisplayName(record.id)),
      ready: record.ready === true,
      hasLocalStats: record.hasLocalStats !== false,
      hasPromptStats: record.hasPromptStats !== false,
      scope: String(record.scope || "device"),
      todayPrompts: numberValue(record.todayPrompts),
      todaySessions: numberValue(record.todaySessions),
      todayTotalTokens: numberValue(record.todayTotalTokens),
      todayTokensByModel: cloneValue(record.todayTokensByModel, ({})),
      recentDays: cloneValue(record.recentDays, []),
      totalPrompts: numberValue(record.totalPrompts),
      totalSessions: numberValue(record.totalSessions),
      activeDays: numberValue(record.activeDays),
      activeDates: cloneValue(record.activeDates, []),
      modelUsage: cloneValue(record.modelUsage, ({})),
      modelUsageByPeriod: mergeModelUsageByPeriod(
        record.modelUsageByPeriod,
        modelHistoryPeriodsFor(String(record.id))
      )
    }
  }

  function localSnapshot() {
    var providerMap = {}
    for (var i = 0; i < agents.length; i++) {
      var record = agents[i] ? agents[i].record : null
      if (!record || !record.id) continue
      if (!providerEnabled(String(record.id))) continue
      providerMap[String(record.id)] = providerSnapshot(record)
    }
    return {
      schemaVersion: 1,
      deviceId: syncEffectiveDeviceId,
      updatedAt: new Date().toISOString(),
      providers: providerMap
    }
  }

  function syncedStatsFor(providerId) {
    var rev = syncRevision
    if (!syncConfigured() || !aggregateData || !aggregateData.providers) return null
    return aggregateData.providers[providerId] || null
  }

  // ---------------------------------------------------------------- format

  function formatTokenCount(n) {
    if (n === undefined || n === null) return "0"
    if (n >= 1e9) return (n / 1e9).toFixed(1) + "B"
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M"
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "K"
    return String(n)
  }

  function modelWordCase(word) {
    if (word === "gpt") return "GPT"
    if (word === "deepseek") return "DeepSeek"
    return word.charAt(0).toUpperCase() + word.slice(1)
  }

  // Model ids arrive hyphenated with the version split across segments
  // (`claude-opus-4-8`, `gpt-5.6-sol`). Rejoin the numeric run into one
  // version and title-case the words around it.
  function friendlyModelName(id) {
    if (!id) return "Unknown"
    var name = String(id).replace(/^claude-/, "").replace(/-\d{8}$/, "")
    var parts = name.split("-")
    var words = []
    var version = []
    for (var i = 0; i < parts.length; i++) {
      var part = parts[i]
      if (part === "") continue
      if (/^\d/.test(part)) {
        version.push(part)
        continue
      }
      if (version.length > 0) {
        words.push(version.join("."))
        version = []
      }
      words.push(modelWordCase(part))
    }
    if (version.length > 0) words.push(version.join("."))
    return words.length > 0 ? words.join(" ") : "Unknown"
  }
}
