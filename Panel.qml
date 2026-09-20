import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "io.github.murali-lns.agents-usage"
  ipcTarget: "io.github.murali-lns.agents-usage"
  manageIpc: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color surface: Color.popups.background
  readonly property color track: Style.selectedFillFor(foreground, Color.accent)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property var providers: usage.enabledProviders
  // The selection follows the provider, not the slot it happens to sit in: a
  // provider whose first scan lands while the panel is open would otherwise
  // shift the list underneath you and swap out what you were reading.
  property string selectedProviderId: ""
  readonly property int providerIndex: {
    for (var i = 0; i < providers.length; i++)
      if (providers[i].providerId === selectedProviderId) return i
    return 0
  }
  readonly property var provider: providers.length > 0 ? providers[providerIndex] : null

  // Hermes subscription-route selection and state
  property string selectedHermesRouteId: "all"
  readonly property var hermesRoutes: (root.provider && root.provider.providerId === "hermes" && Array.isArray(root.provider.routes))
    ? root.provider.routes
    : []
  readonly property var availableHermesRoutes: {
    var out = []
    for (var i = 0; i < hermesRoutes.length; i++) {
      if (hermesRoutes[i] && hermesRoutes[i].id !== "all" && !hermesRoutes[i].isAll)
        out.push(hermesRoutes[i])
    }
    return out
  }
  readonly property bool showHermesRouteBar: !!root.provider
    && root.provider.providerId === "hermes"
    && availableHermesRoutes.length > 0
  readonly property var selectedHermesRoute: {
    if (!root.provider || root.provider.providerId !== "hermes" || hermesRoutes.length === 0)
      return null
    for (var i = 0; i < hermesRoutes.length; i++) {
      if (hermesRoutes[i] && hermesRoutes[i].id === root.selectedHermesRouteId)
        return hermesRoutes[i]
    }
    return hermesRoutes[0]
  }
  readonly property var activeHermesSource: (root.provider && root.provider.providerId === "hermes" && root.selectedHermesRoute)
    ? root.selectedHermesRoute
    : root.provider

  // Default to Today so the first view matches the existing today counters;
  // older records fall back to todayTokensByModel and All time to modelUsage.
  readonly property var modelPeriods: [
    { id: "today", label: "Today" },
    { id: "7d", label: "7 days" },
    { id: "month", label: "1 month" },
    { id: "all", label: "All time" }
  ]
  property string selectedModelPeriodId: "today"

  property bool cursorActive: false

  // Countdowns and "updated" read this instead of Date.now() so the
  // panel keeps telling the truth while it sits open.
  property double nowMs: Date.now()

  readonly property var limits: limitWindows(provider)
  readonly property var models: modelRows(provider)
  readonly property var headline: bindingWindow(provider)
  readonly property var balance: provider ? (provider.balance || null) : null
  // A prepaid account runs low the way a subscription window fills up: the
  // last 10% of the funded credits lights the same alarm.
  readonly property bool balanceAlarming: !!balance && balance.funded > 0
    && balance.remaining / balance.funded <= 0.1
  readonly property bool alarming: (!!headline && headline.percent >= 0.9) || balanceAlarming

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }
  function alpha(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }

  function selectProvider(index) {
    if (providers.length === 0) return
    var wrapped = ((index % providers.length) + providers.length) % providers.length
    selectedProviderId = providers[wrapped].providerId
    selectedHermesRouteId = "all"
  }

  function refreshNow() {
    usage.refreshAll(true)
  }

  function launchAgent() {
    if (root.bar) root.bar.run("omarchy-agent --pick")
    root.close()
  }

  // ---------------------------------------------------------------- limits
  //
  // Both providers report the same two shapes: a short rolling session window
  // and a long weekly one. Everything below normalizes them into one record so
  // the meters and the hero speak a single language.

  // Claude spells its windows out ("Session (5-hour)"), Codex abbreviates
  // them ("5h window", "30m window"). Both have to land on the same record.
  function windowIsLong(text) {
    return text.indexOf("week") >= 0 || text.indexOf("7-day") >= 0 || text.indexOf("seven") >= 0
      || text.indexOf("month") >= 0 || text.indexOf("30-day") >= 0
  }

  function windowSpanMs(label) {
    var text = String(label || "").toLowerCase()
    if (text.indexOf("month") >= 0 || text.indexOf("30-day") >= 0) return 30 * 24 * 3600 * 1000
    if (windowIsLong(text)) return 7 * 24 * 3600 * 1000
    var hours = text.match(/(\d+)\s*-?\s*h(?:our)?\b/)
    if (hours) return Number(hours[1]) * 3600 * 1000
    var minutes = text.match(/(\d+)\s*-?\s*m(?:in(?:ute)?s?)?\b/)
    if (minutes) return Number(minutes[1]) * 60 * 1000
    return 0
  }

  function windowTitle(label) {
    var text = String(label || "").toLowerCase()
    if (text.indexOf("month") >= 0 || text.indexOf("30-day") >= 0) return "Monthly"
    if (text.indexOf("week") >= 0 || text.indexOf("7-day") >= 0 || text.indexOf("seven") >= 0) return "Weekly"
    if (text.indexOf("session") >= 0 || text.indexOf("hour") >= 0 || text.indexOf("5h") >= 0 || windowSpanMs(label) > 0) return "Session"
    var plain = String(label || "").replace(/\s*\(.*\)\s*/, "").trim()
    return plain === "" ? "Limit" : plain
  }

  // A collector that already knows which window a limit belongs to says so,
  // and that beats reading it back out of the label: a model-scoped limit is
  // titled after its model, and a name like "Opus 5 (1M context)" would parse
  // as a one-minute window.
  function limitWindow(raw) {
    if (!raw || typeof raw !== "object") return null
    var label = String(raw.label || "")
    var title = String(raw.title || "")
    var percent = Number(raw.percent)
    var used = raw.used !== undefined && raw.used !== null ? Number(raw.used) : undefined
    var limit = raw.limit !== undefined && raw.limit !== null ? Number(raw.limit) : undefined
    var remaining = raw.remaining !== undefined && raw.remaining !== null ? Number(raw.remaining) : undefined
    if (used !== undefined && !isFinite(used)) used = undefined
    if (limit !== undefined && !isFinite(limit)) limit = undefined
    if (remaining !== undefined && !isFinite(remaining)) remaining = undefined
    if (!isFinite(percent) || percent < 0) {
      if (used !== undefined && limit !== undefined && limit > 0) {
        percent = clamp(used / limit, 0, 1)
      } else {
        percent = -1
      }
    } else {
      percent = clamp(percent, 0, 1)
    }
    return {
      title: title !== "" ? title : windowTitle(label),
      percent: percent,
      resetAt: String(raw.resetsAt || raw.resetAt || ""),
      used: used,
      limit: limit,
      remaining: remaining,
      source: String(raw.source || ""),
      status: String(raw.status || "")
    }
  }

  function limitWindows(p) {
    if (!p) return []
    var out = []
    var list = p.limits || []
    for (var i = 0; i < list.length; i++) {
      var entry = limitWindow(list[i])
      if (entry && (entry.percent >= 0 || entry.used !== undefined || entry.remaining !== undefined)) {
        out.push(entry)
      }
    }
    return out
  }

  // The window that decides how much room is left — the fullest one, since
  // that is what stops the next prompt.
  function bindingWindow(p) {
    var windows = limitWindows(p)
    var best = null
    for (var i = 0; i < windows.length; i++) {
      if (!best || windows[i].percent > best.percent) best = windows[i]
    }
    return best
  }

  function resetMsFor(w) {
    if (!w || w.resetAt === "") return -1
    var ms = new Date(w.resetAt).getTime()
    return isFinite(ms) ? ms - root.nowMs : -1
  }

  function formatDuration(ms) {
    if (!(ms > 0)) return "now"
    var minutes = Math.floor(ms / 60000)
    var hours = Math.floor(minutes / 60)
    var days = Math.floor(hours / 24)
    if (days > 0) return days + "d " + (hours % 24) + "h"
    if (hours > 0) return hours + "h " + (minutes % 60) + "m"
    return Math.max(1, minutes) + "m"
  }

  // ---------------------------------------------------------------- balance
  //
  // Prepaid agents report a credit ledger instead of rate-limit windows: the
  // record's balance object carries remaining, funded, and spent amounts.

  function currencyPrefix(currency) {
    var code = String(currency || "USD").toUpperCase()
    if (code === "USD") return "$"
    if (code === "EUR") return "€"
    if (code === "GBP") return "£"
    return code + " "
  }

  function formatMoney(value, currency) {
    var amount = Number(value)
    if (!isFinite(amount)) amount = 0
    return currencyPrefix(currency) + amount.toFixed(2)
  }

  function balanceDetailText(b) {
    if (!b || !(b.funded > 0)) return ""
    var text = formatMoney(b.spent, b.currency) + " spent of " + formatMoney(b.funded, b.currency) + " funded"
    if (b.estimated) text += " · estimated"
    return text
  }

  // ---------------------------------------------------------------- content

  // The plan you pay for, under the name of the tool it pays for. Limits live
  // in their own section; the hero just says what this is.
  function heroMeta(p) {
    if (!p) return ""
    if (String(p.usageStatusText || "") !== "") return p.usageStatusText
    if (p.providerId === "hermes" && root.selectedHermesRoute) {
      if (root.selectedHermesRoute.id !== "all" && root.selectedHermesRoute.label)
        return root.selectedHermesRoute.label
      return "Usage only"
    }
    var tier = String(p.tierLabel || "")
    if (tier !== "") return tier.charAt(0).toUpperCase() + tier.slice(1)
    if ((p.limits && p.limits.length > 0) || !!p.balance) return "Subscription"
    return "Usage only"
  }

  // Local calendar date, recomputed from nowMs so a panel left open across
  // midnight moves the "Today" row with the clock.
  function todayDate() {
    var now = new Date(root.nowMs)
    return now.getFullYear()
      + "-" + String(now.getMonth() + 1).padStart(2, "0")
      + "-" + String(now.getDate()).padStart(2, "0")
  }

  function dayName(date) {
    var parsed = new Date(String(date || "") + "T00:00:00")
    if (isNaN(parsed.getTime())) return String(date || "")
    return ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][parsed.getDay()]
  }

  function dayLabel(date, today) {
    if (today) return "Today"
    return dayName(date)
  }

  function dayTooltip(day, today) {
    if (!day) return ""
    var parsed = new Date(String(day.date) + "T00:00:00")
    var label = isNaN(parsed.getTime())
      ? String(day.date)
      : dayName(day.date) + " " + (parsed.getMonth() + 1) + "/" + parsed.getDate()
    var text = label + " · " + usage.formatTokenCount(Number(day.messageCount || 0)) + " tokens"
    // Prompt and session counts only exist for today, so they ride along here
    // instead of taking a section of their own. Billing-API agents never
    // count prompts, and "0 prompts" would read as a quiet day, not a gap.
    var activeSource = (provider && provider.providerId === "hermes" && root.selectedHermesRoute)
      ? root.selectedHermesRoute
      : provider
    if (today && activeSource && provider && provider.hasPromptStats !== false)
      text += " · " + Number(activeSource.todayPrompts || 0) + " prompts · "
        + Number(activeSource.todaySessions || 0) + " sessions"
    return text
  }

  function weekPeak(p) {
    var days = []
    if (p && p.providerId === "hermes" && root.selectedHermesRoute) {
      days = root.selectedHermesRoute.recentDays || []
    } else if (p) {
      days = p.recentDays || []
    }
    var peak = 0
    for (var i = 0; i < days.length; i++) peak = Math.max(peak, Number(days[i].messageCount || 0))
    return peak
  }

  // The Today card reads the authoritative current-day bucket; records that
  // never published one fall back to the day's own observed bucket, never to
  // a prorated cumulative total.
  function todayTokenTotal(source) {
    if (!source) return 0
    var total = root.safeTokenNumber(source.todayTotalTokens)
    if (total > 0) return total
    var days = source.recentDays || []
    var today = root.todayDate()
    for (var i = 0; i < days.length; i++) {
      if (String(days[i].date || "") === today) return root.safeTokenNumber(days[i].messageCount)
    }
    return 0
  }

  // The Last 7 Days card sums the same observed daily buckets the chart
  // draws; it never prorates or rescales a cumulative total.
  function weekTokenTotal(days) {
    var total = 0
    var list = days || []
    for (var i = 0; i < list.length; i++) total += root.safeTokenNumber(list[i].messageCount)
    return total
  }

  function dayOfMonth(date) {
    var parsed = new Date(String(date || "") + "T00:00:00")
    if (isNaN(parsed.getTime())) return ""
    return String(parsed.getDate())
  }

  function validModelPeriod(id) {
    return id === "today" || id === "7d" || id === "month" || id === "all"
  }

  function modelPeriodLabel(id) {
    for (var i = 0; i < root.modelPeriods.length; i++) {
      if (root.modelPeriods[i].id === id) return root.modelPeriods[i].label
    }
    return "Today"
  }

  function modelUsageSourceFor(p) {
    if (!p) return null
    if (p.providerId === "hermes" && root.selectedHermesRoute) return root.selectedHermesRoute
    return p
  }

  function modelPeriodValues(source, periodId) {
    if (!source || !validModelPeriod(periodId)) return null
    var byPeriod = source.modelUsageByPeriod
    if (byPeriod && typeof byPeriod === "object" && !Array.isArray(byPeriod)
        && byPeriod[periodId] !== undefined && byPeriod[periodId] !== null
        && typeof byPeriod[periodId] === "object" && !Array.isArray(byPeriod[periodId])) {
      return byPeriod[periodId]
    }
    // Legacy records did not have modelUsageByPeriod. Keep their Today and
    // All time views useful without manufacturing missing 7-day/month data.
    if (periodId === "today" && source.todayTokensByModel
        && typeof source.todayTokensByModel === "object" && !Array.isArray(source.todayTokensByModel))
      return source.todayTokensByModel
    if (periodId === "all" && source.modelUsage
        && typeof source.modelUsage === "object" && !Array.isArray(source.modelUsage))
      return source.modelUsage
    return null
  }

  function safeTokenNumber(value) {
    if (typeof value !== "number" && typeof value !== "string") return 0
    var n = Number(value)
    return isFinite(n) && n > 0 ? n : 0
  }

  function tokenParts(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return {
        total: safeTokenNumber(value),
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        hasBreakdown: false
      }
    }
    var input = safeTokenNumber(value.inputTokens)
    var output = safeTokenNumber(value.outputTokens)
    var cacheRead = safeTokenNumber(value.cacheReadInputTokens)
    var cacheWrite = safeTokenNumber(value.cacheCreationInputTokens)
    var total = input + output + cacheRead + cacheWrite
    if (total === 0 && value.totalTokens !== undefined) total = safeTokenNumber(value.totalTokens)
    return {
      total: total,
      input: input,
      output: output,
      cacheRead: cacheRead,
      cacheWrite: cacheWrite,
      hasBreakdown: value.inputTokens !== undefined || value.outputTokens !== undefined
        || value.cacheReadInputTokens !== undefined || value.cacheCreationInputTokens !== undefined
    }
  }

  function modelSourceAvailable(p) {
    return modelPeriodValues(modelUsageSourceFor(p), root.selectedModelPeriodId) !== null
  }

  function modelUnavailableText(p) {
    if (!p) return ""
    if (!modelSourceAvailable(p))
      return "Per-model token source unavailable for " + modelPeriodLabel(root.selectedModelPeriodId) + "."
    if (root.models.length === 0)
      return "No model-token usage recorded for " + modelPeriodLabel(root.selectedModelPeriodId) + "."
    return ""
  }

  function modelRows(p) {
    var source = modelUsageSourceFor(p)
    var usageByModel = modelPeriodValues(source, root.selectedModelPeriodId)
    if (!usageByModel || typeof usageByModel !== "object" || Array.isArray(usageByModel)) return []
    var rows = []
    for (var id in usageByModel) {
      var modelId = String(id)
      if (modelId.trim() === "") continue
      var parts = tokenParts(usageByModel[id])
      if (!(parts.total > 0) || !isFinite(parts.total)) continue
      rows.push({
        id: modelId,
        name: usage.friendlyModelName(modelId),
        total: parts.total,
        input: parts.input,
        output: parts.output,
        cacheRead: parts.cacheRead,
        cacheWrite: parts.cacheWrite,
        hasBreakdown: parts.hasBreakdown
      })
    }
    rows.sort(function(a, b) { return b.total - a.total })
    return rows
  }

  function modelShare(row, rows) {
    var total = row ? Number(row.total) : 0
    var peak = rows && rows.length > 0 ? Number(rows[0].total) : 0
    if (!isFinite(total) || total <= 0 || !isFinite(peak) || peak <= 0) return 0
    return clamp(total / peak, 0, 1)
  }

  function modelTooltip(row) {
    if (!row) return ""
    if (row.hasBreakdown !== true)
      return "Total " + usage.formatTokenCount(row.total) + " tokens"
    return "In " + usage.formatTokenCount(row.input)
      + " · out " + usage.formatTokenCount(row.output)
      + " · cache read " + usage.formatTokenCount(row.cacheRead)
      + " · cache write " + usage.formatTokenCount(row.cacheWrite)
  }

  // Only speaks up when the numbers cover more than this machine.
  function footerText() {
    if (usage.syncStatusText !== "") return usage.syncStatusText
    if (provider && provider.syncEnabled && provider.syncDeviceCount > 0)
      return "Merged from " + provider.syncDeviceCount + " device" + (provider.syncDeviceCount === 1 ? "" : "s")
    return ""
  }

  // Agents that ship a white mark carry an `assets/<id>-light.svg` twin for
  // light surfaces; marks that work on both (Claude's brand-orange) ship one
  // file. The luminance check decides which candidate to try first.
  function colorChannelLuminance(value) {
    var channel = Number(value)
    if (!isFinite(channel)) return 0
    return channel <= 0.03928 ? channel / 12.92 : Math.pow((channel + 0.055) / 1.055, 2.4)
  }

  function colorLuminance(color) {
    return 0.2126 * colorChannelLuminance(color.r)
      + 0.7152 * colorChannelLuminance(color.g)
      + 0.0722 * colorChannelLuminance(color.b)
  }

  // Marks resolve by convention, so a new agent's data file needs nothing
  // from this panel: assets/<id>.svg if it ships one, the module's bar glyph
  // if it doesn't.
  function iconCandidatesForProvider(p, surfaceColor) {
    if (!p) return []
    var candidates = []
    if (colorLuminance(surfaceColor || Color.background) >= 0.5)
      candidates.push(Qt.resolvedUrl("assets/" + p.providerId + "-light.svg"))
    candidates.push(Qt.resolvedUrl("assets/" + p.providerId + ".svg"))
    return candidates
  }

  // Nothing to report, nothing in the bar: Bar.qml collapses a slot whose item
  // is invisible, so the icon appears the moment the first scan finds usage and
  // stays away entirely on a machine that has never produced a usage record.
  visible: providers.length > 0
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onProviderIndexChanged: if (panelFlick) panelFlick.contentY = 0
  onOpenedChanged: if (opened) {
    cursorActive = false
    nowMs = Date.now()
    if (panelFlick) panelFlick.contentY = 0
    usage.refreshLimits()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Main {
    id: usage
    settings: root.settings
  }

  // Cheap enough to keep running: it only re-evaluates text bindings, and a
  // stale "resets in 2h" on a panel that is open is worse than a timer.
  Timer {
    interval: 30000
    running: root.opened
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refreshNow(); return "ok" }
    function next(): string { root.selectProvider(root.providerIndex + 1); return "ok" }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󱚣"
    active: root.alarming
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.launchAgent()
      else if (buttonCode === Qt.MiddleButton) root.selectProvider(root.providerIndex + 1)
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    // Taller than the control panels on purpose: this one is a dashboard, and
    // the whole point is reading limits and history without scrolling.
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      onMoveRequested: function(dx, dy) {
        if (dx !== 0) {
          root.cursorActive = true
          root.selectProvider(root.providerIndex + dx)
        }
        if (dy !== 0)
          panelFlick.contentY = root.clamp(panelFlick.contentY + dy * Style.space(56), 0,
                                           Math.max(0, panelFlick.contentHeight - panelFlick.height))
      }
      onActivateRequested: root.refreshNow()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) { if (t === "r" || t === "R") root.refreshNow() }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          // ---------- Hero: provider mark · name · plan ----------
          PanelHero {
            id: hero
            visible: !!root.provider
            width: parent.width
            title: root.provider ? root.provider.providerName : ""
            meta: root.heroMeta(root.provider)
            foreground: root.foreground
            fontFamily: root.fontFamily

            iconComponent: Component {
              Item {
                id: heroMark
                property var candidates: root.iconCandidatesForProvider(root.provider, root.surface)
                // Provider objects are rebuilt on every refresh, which churns the
                // array's identity without changing its content. Restart the fallback
                // walk only when the URLs change: re-pointing source at a URL whose
                // load already failed emits no statusChanged, so an identity-only
                // reset would strand the walker on a missing -light twin.
                property string candidatesKey: candidates.join("\n")
                property int candidateIndex: 0
                onCandidatesKeyChanged: candidateIndex = 0

                width: Style.font.display
                height: Style.font.display

                Image {
                  id: heroMarkImage
                  anchors.fill: parent
                  source: heroMark.candidateIndex < heroMark.candidates.length ? heroMark.candidates[heroMark.candidateIndex] : ""
                  sourceSize.width: Style.font.display * 2
                  sourceSize.height: Style.font.display * 2
                  fillMode: Image.PreserveAspectFit
                  // Advancing source from inside its own status change trips the
                  // binding-loop detector; defer the step one tick.
                  onStatusChanged: if (status === Image.Error && heroMark.candidateIndex < heroMark.candidates.length)
                    Qt.callLater(function() { heroMark.candidateIndex++ })
                }

                Text {
                  textFormat: Text.PlainText
                  anchors.centerIn: parent
                  visible: heroMarkImage.status !== Image.Ready
                  text: button.text
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.display
                }
              }
            }
          }

          Text {
            visible: root.providers.length === 0
            width: parent.width
            topPadding: Style.space(24)
            text: "No AI coding subscriptions found.\nAgents show up here once you've used them."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
          }

          // ---------- Provider switch ----------
          Row {
            id: providerSwitch
            visible: root.providers.length > 1
            width: parent.width
            spacing: Style.spacing.md

            readonly property real cellWidth: root.providers.length > 0
              ? (width - spacing * (root.providers.length - 1)) / root.providers.length
              : 0

            Repeater {
              model: root.providers

              Button {
                required property var modelData
                required property int index

                width: providerSwitch.cellWidth
                text: modelData.providerName
                selected: index === root.providerIndex
                hasCursor: root.cursorActive && index === root.providerIndex
                bordered: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                verticalPadding: Style.spacing.controlPaddingY
                onClicked: {
                  root.cursorActive = true
                  root.selectProvider(index)
                }
                onHovered: function(isHovered) { if (isHovered) root.cursorActive = true }
              }
            }
          }

          // ---------- Hermes subscription route switch ----------
          Flow {
            id: hermesRouteBar
            visible: root.showHermesRouteBar
            width: parent.width
            spacing: Style.spacing.sm

            Repeater {
              model: root.hermesRoutes

              Button {
                required property var modelData
                required property int index

                text: modelData.label || "Route"
                selected: root.selectedHermesRoute
                  ? root.selectedHermesRoute.id === modelData.id
                  : (index === 0)
                bordered: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                verticalPadding: Style.space(4)
                horizontalPadding: Style.space(8)
                onClicked: {
                  root.selectedHermesRouteId = String(modelData.id)
                }
              }
            }
          }

          // ---------- Status ----------
          BorderSurface {
            // The banner flags a problem and shows how to fix it: it needs a
            // status worth flagging AND guidance text, so usage-only states
            // and stale help text no longer render an empty or false alert.
            visible: !!root.provider
              && String(root.provider.usageStatusText || "") !== ""
              && String(root.provider.authHelpText || "") !== ""
            width: parent.width
            implicitHeight: statusText.implicitHeight + Style.spacing.xl * 2
            color: root.alpha(root.urgent, 0.10)
            borderSpec: Border.flat(root.alpha(root.urgent, 0.35), 1)
            radius: Style.cornerRadius

            Text {
              id: statusText
              textFormat: Text.PlainText
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(12)
              anchors.rightMargin: Style.space(12)
              text: root.provider ? String(root.provider.authHelpText || "") : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          // ---------- Balance / limits ----------
          PanelSeparator {
            visible: balanceSection.visible || limitsSection.visible
            foreground: root.foreground
          }

          Column {
            id: balanceSection
            visible: !!root.balance
            width: parent.width
            spacing: Style.space(10)

            // The meter shows what is left, not what is used: a prepaid
            // account drains toward empty rather than filling toward a cap.
            readonly property real ratio: root.balance && root.balance.funded > 0
              ? root.clamp(root.balance.remaining / root.balance.funded, 0, 1)
              : -1

            PanelSectionHeader {
              width: parent.width
              text: "BALANCE"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Item {
              width: parent.width
              implicitHeight: Math.max(balanceLabel.implicitHeight, balanceValue.implicitHeight)

              Text {
                id: balanceLabel
                text: "Prepaid credits"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                id: balanceValue
                textFormat: Text.PlainText
                text: root.balance ? root.formatMoney(root.balance.remaining, root.balance.currency) : ""
                color: root.balanceAlarming ? root.urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
              }
            }

            Meter {
              visible: balanceSection.ratio >= 0
              width: parent.width
              value: balanceSection.ratio
              alarming: root.balanceAlarming
            }

            Text {
              textFormat: Text.PlainText
              visible: text !== ""
              width: parent.width
              text: root.balanceDetailText(root.balance)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Column {
            id: limitsSection
            visible: root.limits.length > 0 || (!!root.provider && !root.balance)
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: "LIMITS"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Repeater {
              model: root.limits

              LimitRow {
                required property var modelData
                width: limitsSection.width
                window: modelData
              }
            }

            Text {
              id: limitsUnavailableText
              visible: root.limits.length === 0
              textFormat: Text.PlainText
              width: parent.width
              text: "Limit unavailable · Usage only"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          // ---------- Hermes Activity (API calls, sessions, active days) ----------
          PanelSeparator {
            visible: hermesActivitySection.visible
            foreground: root.foreground
          }

          Column {
            id: hermesActivitySection
            visible: !!root.provider && root.provider.providerId === "hermes"
            width: parent.width
            spacing: Style.space(8)

            readonly property var activeSource: root.activeHermesSource
            readonly property int apiCalls: activeSource ? Number(activeSource.apiCallCount || activeSource.totalPrompts || 0) : 0
            readonly property int totalSessions: activeSource ? Number(activeSource.sessions || activeSource.totalSessions || 0) : 0
            readonly property int activeDaysCount: activeSource ? Number(activeSource.activeDays || 0) : 0

            PanelSectionHeader {
              width: parent.width
              text: "ACTIVITY"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Row {
              width: parent.width
              spacing: Style.spacing.md

              readonly property real cellWidth: (width - spacing * 2) / 3

              Column {
                width: parent.cellWidth
                spacing: Style.space(2)
                Text {
                  textFormat: Text.PlainText
                  text: "API Calls"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
                Text {
                  textFormat: Text.PlainText
                  text: usage.formatTokenCount(hermesActivitySection.apiCalls)
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                }
              }

              Column {
                width: parent.cellWidth
                spacing: Style.space(2)
                Text {
                  textFormat: Text.PlainText
                  text: "Sessions"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
                Text {
                  textFormat: Text.PlainText
                  text: String(hermesActivitySection.totalSessions)
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                }
              }

              Column {
                width: parent.cellWidth
                spacing: Style.space(2)
                Text {
                  textFormat: Text.PlainText
                  text: "Active Days"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
                Text {
                  textFormat: Text.PlainText
                  text: String(hermesActivitySection.activeDaysCount)
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                }
              }
            }
          }

          // ---------- Usage ----------
          PanelSeparator {
            visible: usageSection.visible
            foreground: root.foreground
          }

          Column {
            id: usageSection
            readonly property var source: root.activeHermesSource
            readonly property var days: source ? (source.recentDays || []) : []
            readonly property real peak: Math.max(1, root.weekPeak(root.provider))
            readonly property real todayTokens: root.todayTokenTotal(source)
            readonly property real weekTokens: root.weekTokenTotal(days)
            visible: !!root.provider && days.length > 0
            width: parent.width
            spacing: Style.spacing.md

            // Period cards: today carries the accent fill so the current day
            // reads first; the seven-day total sits quieter beside it.
            Row {
              id: usageCards
              width: parent.width
              spacing: Style.spacing.lg

              readonly property real cardWidth: (width - spacing) / 2

              UsageCard {
                width: usageCards.cardWidth
                title: "Today"
                value: usageSection.todayTokens
                highlighted: true
              }

              UsageCard {
                width: usageCards.cardWidth
                title: "Last 7 Days"
                value: usageSection.weekTokens
                highlighted: false
              }
            }

            // Daily activity: one column per day, scaled to the week's peak.
            Item {
              width: parent.width
              implicitHeight: Math.max(dailyActivityHeader.implicitHeight, dailyActivitySpan.implicitHeight)

              PanelSectionHeader {
                id: dailyActivityHeader
                text: "DAILY ACTIVITY"
                foreground: root.foreground
                fontFamily: root.fontFamily
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                id: dailyActivitySpan
                textFormat: Text.PlainText
                text: usageSection.days.length + " days"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
              }
            }

            Row {
              id: dailyActivityChart
              width: parent.width
              spacing: 0

              Repeater {
                model: usageSection.days

                DayColumn {
                  required property var modelData
                  required property int index

                  width: dailyActivityChart.width / Math.max(1, usageSection.days.length)
                  day: modelData
                  peak: usageSection.peak
                  // By date, not by position: the Claude stats-cache fallback
                  // can hand us a window that stops short of today.
                  today: String(modelData.date || "") === root.todayDate()
                }
              }
            }
          }

          // ---------- Models ----------
          PanelSeparator {
            visible: modelSection.visible
            foreground: root.foreground
          }

          Column {
            id: modelSection
            visible: !!root.provider
            width: parent.width
            spacing: Style.spacing.md

            PanelSectionHeader {
              width: parent.width
              text: "TOKENS BY MODEL"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Flow {
              id: modelPeriodFilter
              width: parent.width
              spacing: Style.spacing.sm

              Repeater {
                model: root.modelPeriods

                Button {
                  required property var modelData

                  text: modelData.label
                  selected: root.selectedModelPeriodId === String(modelData.id)
                  hasCursor: root.cursorActive && selected
                  bordered: true
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  verticalPadding: Style.space(4)
                  horizontalPadding: Style.space(8)
                  onClicked: {
                    root.cursorActive = true
                    root.selectedModelPeriodId = String(modelData.id)
                  }
                  onHovered: function(isHovered) { if (isHovered) root.cursorActive = true }
                }
              }
            }

            Repeater {
              model: root.models

              ModelRow {
                required property var modelData
                width: modelSection.width
                row: modelData
                // Scaled to the heaviest model, so the top row is always full —
                // the same scale-to-peak the weekly chart uses for its busiest day.
                share: root.modelShare(modelData, root.models)
              }
            }

            Text {
              id: modelUnavailableLabel
              visible: text !== ""
              textFormat: Text.PlainText
              width: parent.width
              text: root.modelUnavailableText(root.provider)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: text !== ""
            width: parent.width
            topPadding: Style.space(2)
            text: root.footerText()
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
            elide: Text.ElideRight
          }
        }
      }
    }
  }

  // A limit window: label and percentage, meter, and reset countdown.
  component LimitRow: Column {
    id: limitRow
    property var window: null

    readonly property bool alarming: window && window.percent >= 0.9

    spacing: Style.space(6)

    Item {
      width: parent.width
      implicitHeight: Math.max(limitLabel.implicitHeight, limitValue.implicitHeight)

      Text {
        id: limitLabel
        textFormat: Text.PlainText
        // A model-scoped window is titled after its model, and those names run
        // long enough to reach the percentage, so the title gives way first.
        text: limitRow.window ? limitRow.window.title : ""
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        elide: Text.ElideRight
        anchors.left: parent.left
        anchors.right: limitValue.left
        anchors.rightMargin: Style.spacing.sm
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        id: limitValue
        textFormat: Text.PlainText
        text: {
          if (!limitRow.window) return "—"
          if (limitRow.window.percent >= 0) return Math.round(limitRow.window.percent * 100) + "%"
          if (limitRow.window.remaining !== undefined) return usage.formatTokenCount(limitRow.window.remaining) + " left"
          return "—"
        }
        color: limitRow.alarming ? root.urgent : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    Meter {
      visible: limitRow.window && limitRow.window.percent >= 0
      width: parent.width
      value: limitRow.window ? limitRow.window.percent : -1
      alarming: limitRow.alarming
    }

    Text {
      id: resetText
      textFormat: Text.PlainText
      visible: text !== ""
      width: parent.width
      text: {
        var parts = []
        if (limitRow.window && limitRow.window.used !== undefined && limitRow.window.limit !== undefined) {
          parts.push(usage.formatTokenCount(limitRow.window.used) + " / " + usage.formatTokenCount(limitRow.window.limit) + " used")
        } else if (limitRow.window && limitRow.window.remaining !== undefined && limitRow.window.percent >= 0) {
          parts.push(usage.formatTokenCount(limitRow.window.remaining) + " remaining")
        }
        var remainingMs = root.resetMsFor(limitRow.window)
        if (remainingMs > 0) {
          parts.push("Resets in " + root.formatDuration(remainingMs))
        }
        if (limitRow.window && limitRow.window.status) {
          parts.push(limitRow.window.status)
        }
        return parts.join(" · ")
      }
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }

  // Rounded track showing the percentage of the allowance used.
  component Meter: Item {
    id: meter
    property real value: -1
    property bool alarming: false
    property real thickness: Math.max(Style.space(4), Math.round(Style.spacing.controlHeight * 0.14))

    implicitHeight: thickness

    Rectangle {
      id: meterTrack
      anchors.fill: parent
      radius: height / 2
      color: root.track
    }

    Rectangle {
      anchors.left: meterTrack.left
      anchors.verticalCenter: meterTrack.verticalCenter
      height: meterTrack.height
      radius: meterTrack.radius
      width: meterTrack.width * root.clamp(meter.value, 0, 1)
      color: meter.alarming ? root.urgent : root.foreground

      Behavior on width {
        NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
      }
    }

  }

  // A period summary card: period title, token caption, the total, and the
  // observed-subtotal note. Today carries the accent fill so the current
  // period reads first; Last 7 Days stays quiet.
  component UsageCard: BorderSurface {
    id: usageCard
    property string title: ""
    property real value: 0
    property bool highlighted: false

    implicitHeight: cardBody.implicitHeight + Style.spacing.xl * 2
    color: usageCard.highlighted ? root.track : root.alpha(root.foreground, 0.05)
    borderSpec: Border.flat(usageCard.highlighted
      ? root.alpha(root.foreground, 0.28)
      : root.alpha(root.foreground, 0.14), 1)
    radius: Style.cornerRadius

    Column {
      id: cardBody
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(2)

      Item {
        width: parent.width
        implicitHeight: Math.max(cardTitle.implicitHeight, cardCaption.implicitHeight)

        Text {
          id: cardTitle
          textFormat: Text.PlainText
          text: usageCard.title
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.bold: true
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
        }

        Text {
          id: cardCaption
          textFormat: Text.PlainText
          text: "TOKENS"
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      Text {
        textFormat: Text.PlainText
        text: usage.formatTokenCount(usageCard.value)
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.display
        font.bold: true
      }

      Text {
        textFormat: Text.PlainText
        // The mock's longer "observed subtotal" phrasing overflows the card at
        // large font bases; these two words carry the same provenance note.
        text: "Partial · observed"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
    }
  }

  // One column of the daily activity chart: the token count rides just above
  // its bar, the weekday and date sit below. Today is picked out in full
  // foreground so the week reads as a run-up to right now; a quiet day keeps
  // a baseline stub instead of a bar and label.
  component DayColumn: Item {
    id: dayColumn
    property var day: null
    property real peak: 1
    property bool today: false

    readonly property real value: dayColumn.day ? Number(dayColumn.day.messageCount || 0) : 0
    readonly property real ratio: dayColumn.peak > 0
      ? root.clamp(dayColumn.value / dayColumn.peak, 0, 1)
      : 0
    readonly property real barMax: Style.space(64)
    readonly property real barWidth: Math.max(Style.space(10),
      Math.min(Math.round(dayColumn.width * 0.5), Style.space(28)))
    readonly property real barHeight: dayColumn.value > 0
      ? Math.max(Style.space(4), Math.round(dayColumn.ratio * dayColumn.barMax))
      : Style.space(2)
    readonly property real valueRowHeight: Math.round(Style.font.caption * 1.6)
    readonly property real labelGap: Style.space(6)

    implicitHeight: valueRowHeight + labelGap + barMax + labelGap + labelBlock.implicitHeight

    Text {
      id: valueText
      textFormat: Text.PlainText
      text: dayColumn.value > 0 ? usage.formatTokenCount(dayColumn.value) : ""
      color: dayColumn.today ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
      anchors.horizontalCenter: parent.horizontalCenter
      // Sits just above its bar's top, like the bar grew into its label.
      y: Math.max(0, bar.y - height - Style.space(3))
    }

    Rectangle {
      id: bar
      width: dayColumn.barWidth
      height: dayColumn.barHeight
      radius: Math.min(Style.space(3), Math.round(height / 2))
      color: dayColumn.today ? root.foreground
        : dayColumn.value > 0 ? root.alpha(root.foreground, 0.5)
        : root.alpha(root.foreground, 0.2)
      anchors.horizontalCenter: parent.horizontalCenter
      y: parent.height - labelBlock.implicitHeight - dayColumn.labelGap - height

      Behavior on height {
        NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
      }
    }

    Column {
      id: labelBlock
      width: parent.width
      anchors.bottom: parent.bottom
      spacing: 0

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: root.dayLabel(dayColumn.day ? dayColumn.day.date : "", dayColumn.today)
        color: dayColumn.today ? root.foreground : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: dayColumn.today
        horizontalAlignment: Text.AlignHCenter
      }

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: root.dayOfMonth(dayColumn.day ? dayColumn.day.date : "")
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        horizontalAlignment: Text.AlignHCenter
      }
    }

    MouseArea {
      id: dayHover
      anchors.fill: parent
      hoverEnabled: true
      acceptedButtons: Qt.NoButton
    }

    PanelToolTip {
      visible: dayHover.containsMouse
      text: root.dayTooltip(dayColumn.day, dayColumn.today)
      fontFamily: root.fontFamily
    }
  }

  // Model rows read as a table: the share bar fills the row behind the label
  // instead of stacking under it, which keeps the whole dashboard on one screen.
  component ModelRow: Item {
    id: modelRow
    property var row: null
    property real share: 0

    implicitHeight: modelName.implicitHeight + Style.spacing.lg

    Rectangle {
      anchors.fill: parent
      radius: Style.cornerRadius
      color: root.alpha(root.foreground, 0.05)
    }

    Rectangle {
      anchors.left: parent.left
      anchors.top: parent.top
      anchors.bottom: parent.bottom
      width: parent.width * root.clamp(modelRow.share, 0, 1)
      radius: Style.cornerRadius
      color: root.alpha(root.foreground, 0.14)

      Behavior on width {
        NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
      }
    }

    Text {
      id: modelName
      textFormat: Text.PlainText
      text: modelRow.row ? modelRow.row.name : ""
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideRight
      anchors.left: parent.left
      anchors.leftMargin: Style.space(8)
      anchors.right: modelTokens.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      id: modelTokens
      textFormat: Text.PlainText
      text: modelRow.row ? usage.formatTokenCount(modelRow.row.total) : ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
      anchors.right: parent.right
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
    }

    MouseArea {
      id: modelHover
      anchors.fill: parent
      hoverEnabled: true
      acceptedButtons: Qt.NoButton
    }

    PanelToolTip {
      visible: modelHover.containsMouse
      text: root.modelTooltip(modelRow.row)
      fontFamily: root.fontFamily
    }
  }
}
