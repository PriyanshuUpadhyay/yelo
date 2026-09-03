import Foundation
import Combine

struct MeterRow: Codable, Equatable {
    let label: String
    let provider: String
    let window: String?
    let pct: Int?
    let reset: String?
    let state: String
    let reason: String?
    /// When this row's value last MOVED — history dedup keys on it, so an unchanged re-read is not
    /// mistaken for a new sample. NOT a freshness signal: a weekly window can sit unmoved for days
    /// while being confirmed continuously.
    let asOf: Double?
    /// When this row's value was last CONFIRMED current. Everything answering "how old is what I'm
    /// looking at?" reads this. Defaulted so fixtures that don't care can omit it.
    var seenAt: Double? = nil
    /// true/false = this Claude profile is/isn't the most-recently-used one; nil (codex rows,
    /// or a data script that predates the flag) = no claim, treat as included everywhere.
    let active: Bool?
    /// Winning cache family for this row (`api`, `statusline`, or `rollout`). Optional for fixtures
    /// and older data scripts; used only to explain freshness in the expanded panel.
    var source: String? = nil
    /// Whether the unified on-demand fetch can refresh this provider's row.
    /// nil (fixtures, older data scripts) = no claim, and the UI fails CLOSED: it promises no fetch.
    var canFetch: Bool? = nil
    /// Prime Agent login state for the Codex identity that shares this quota row.
    var primeSignedIn: Bool? = nil
}

extension MeterRow {
    /// The data script gates each row on the age of its own source data. `ok` means the percentage
    /// describes the account right now; `stale` may carry the last confirmed percentage for display,
    /// while `missing` has no value. Never feed a non-live row into history, pressure, or status.
    var isLive: Bool { state == "ok" }
    /// Data exists but is too old to present as current.
    var isStale: Bool { state == "stale" }
}

/// What the app runs a subprocess FOR: one snapshot read, or one refresh.
enum UsageProcessKind {
    case snapshot
    case fetch
}

/// The executable and arguments for one of those two jobs, derived from the environment
/// alone — no process is launched here, so the contract is testable on its own.
///
/// jello owns the usage pipeline, so the default is `jello usage show --json` and `jello
/// usage fetch`. launchd hands the job no PATH, which is why the LaunchAgent supplies the
/// absolute path in `JELLO_BIN` and `~/.local/bin/jello` is only the fallback for a
/// hand-started run. Each override keeps the contract it always had — path plus `--json`
/// for the data feed, path alone for the fetch — so the fixture scripts still drive the
/// app unchanged, and each one is scoped to its own kind.
func processArguments(
    kind: UsageProcessKind,
    environment: [String: String]
) -> (executable: String, arguments: [String]) {
    switch kind {
    case .snapshot:
        if let script = environment["USAGE_HUD_SCRIPT"] {
            return (script, ["--json"])
        }
        return (jelloPath(environment: environment), ["usage", "show", "--json"])
    case .fetch:
        if let script = environment["USAGE_HUD_FETCH_SCRIPT"] {
            return (script, [])
        }
        return (jelloPath(environment: environment), ["usage", "fetch"])
    }
}

private func jelloPath(environment: [String: String]) -> String {
    environment["JELLO_BIN"] ?? ("~/.local/bin/jello" as NSString).expandingTildeInPath
}

/// Polls the usage provider's `--json` snapshot mode. All token/keychain handling lives
/// behind that command; this process only ever sees percentages and reset strings.
final class UsageModel: ObservableObject {
    @Published var rows: [MeterRow] = []
    /// Oldest source-data timestamp in the accepted snapshot.
    @Published var lastFetchAt: Date?
    /// When the local data snapshot was read; reset strings are relative to this moment.
    @Published var lastSnapshotAt: Date?
    @Published var fetchFailed: Bool = false
    @Published var isRefreshing: Bool = false
    /// True while an on-demand API fetch is in flight — drives the fetch
    /// button's spinner and guards against double-taps.
    @Published var isFetching: Bool = false

    /// No rows AND a failed fetch = the FIRST-EVER fetch failed (never had data to show) — distinct
    /// from a mid-run failure, which keeps last-known rows on screen.
    var firstLaunchFailure: Bool { rows.isEmpty && fetchFailed }

    /// Bubble/expanded state. Transient (not persisted) — driven purely by hover, so the app
    /// always launches collapsed regardless of how it was left last time.
    @Published var isCollapsed: Bool = true

    /// Expanded surface's actual fitted content height, reported by ExpandedContent's GeometryReader
    /// and clamped to `hudPanelSize.height` (the invisible window's hard ceiling). Defaults to that
    /// same ceiling so the very first expand, before any measurement has landed, behaves like the old
    /// fixed-height panel instead of guessing.
    @Published var expandedContentHeight: CGFloat = hudPanelSize.height
    /// Same pattern as `expandedContentHeight`, for width — the widest content row instead of the
    /// fixed panel width, so the expanded card hugs its content on both axes.
    @Published var expandedContentWidth: CGFloat = hudPanelSize.width
    @Published var notchTopInset: CGFloat = fallbackNotchSize.height
    @Published var notchTriggerWidth: CGFloat = fallbackNotchSize.width

    /// Trend history + derived pressure, recomputed on every successful fetch.
    @Published var history: [HistorySample] = []
    @Published var rowPressures: [RowPressure] = []
    @Published var statusText: String?

    var bindingPressure: RowPressure? { bindingRow(rowPressures) }

    /// True when a ceiling change happened while expanded: the stored `expandedContentHeight` was
    /// the visible surface then and couldn't be reset, so the reset is OWED and applied at the
    /// next collapsed ceiling application instead of dropped.
    private var pendingCeilingReset = false

    /// Single owner of the ceiling ↔ stored-height invariant. Writes the new ceiling, then resets
    /// `expandedContentHeight` to it (the same pre-first-measurement default as launch, re-clamped
    /// by the next expand's measurement) — but only while collapsed: while expanded the stored
    /// value IS the visible surface and must stay measured. A skipped reset stays pending rather
    /// than dropped; an unchanged ceiling with nothing pending is a no-op, so routine same-screen
    /// repositions never churn a measured height.
    func applyPanelCeiling(_ height: CGFloat) {
        if height != hudPanelSize.height {
            hudPanelSize.height = height
            pendingCeilingReset = true
        }
        guard pendingCeilingReset, isCollapsed else { return }
        pendingCeilingReset = false
        expandedContentHeight = height
    }

    private let historyStore = HistoryStore()

    private var timer: Timer?
    // A refresh requested while one was already in flight (see refresh()); the follow-up runs when
    // the in-flight one completes so a post-fetch refresh can't be lost to a racing poll. Main-only.
    private var refreshQueued = false
    private var statusClear: DispatchWorkItem?
    private let snapshotCommand = processArguments(
        kind: .snapshot, environment: ProcessInfo.processInfo.environment)
    private let fetchCommand = processArguments(
        kind: .fetch, environment: ProcessInfo.processInfo.environment)
    private let pollInterval: TimeInterval = 120
    // The script is local-only (cache reads + rollout scans, no network) — 8s is generous headroom.
    private let processTimeout: TimeInterval = 8
    // Provider requests run concurrently, but a slow credential renewal can still take up to a
    // minute. A kill at this ceiling
    // truncates the script's output, so runFetchScript reports incomplete rather than parsing the
    // partial status lines as a full result.
    private let fetchTimeout: TimeInterval = 120

    func start() {
        history = historyStore.load()
        rowPressures = UsageHUD.rowPressures(rows: rows, history: history)
        refresh()
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: pollInterval, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    func refresh() {
        // A refresh already in flight may have read the caches BEFORE an in-progress write (e.g. the
        // post-fetch refresh racing a periodic poll) — so don't silently drop this one: queue a
        // follow-up that runs once the in-flight refresh finishes, guaranteeing a read of the latest
        // caches instead of stale data until the next 120s poll.
        guard !isRefreshing else { refreshQueued = true; return }
        isRefreshing = true
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let result = self.fetchSnapshot()
            DispatchQueue.main.async {
                switch result {
                case .success(let rows):
                    let now = Date()
                    self.rows = rows
                    self.lastFetchAt = oldestDataDate(in: rows)
                    self.lastSnapshotAt = now
                    self.fetchFailed = false
                    self.history = self.historyStore.append(samplesFromRows(rows, now: now), now: now)
                    self.rowPressures = UsageHUD.rowPressures(rows: rows, history: self.history)
                case .failure:
                    // Keep last-good rows on screen; only flip the failure marker.
                    self.fetchFailed = true
                }
                self.isRefreshing = false
                if self.refreshQueued {
                    self.refreshQueued = false
                    self.refresh()
                }
            }
        }
    }

    /// Show a status message for 2s; a newer message replaces it and resets the timer.
    func showStatus(_ text: String) {
        statusText = text
        statusClear?.cancel()
        let item = DispatchWorkItem { [weak self] in self?.statusText = nil }
        statusClear = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 2, execute: item)
    }

    /// Runs the usage fetch script off-main, then re-reads the local data feed so the new numbers land.
    /// `isFetching` guards double-taps and drives the button spinner.
    func fetchFromAPI() {
        guard !isFetching else { return }
        isFetching = true
        hudLog("[fetch] start")
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let summary = self.runFetchScript()
            DispatchQueue.main.async {
                self.isFetching = false
                hudLog("[fetch] result \(summary)")
                self.showStatus("fetch · \(summary)")
                // Caches may have been rewritten — re-read the data feed to surface the new numbers.
                self.refresh()
            }
        }
    }

    /// Runs the fetch script, returning a compact one-line summary of its per-profile status lines
    /// ("<name>: <status>") — or "failed" if it couldn't run / produced nothing.
    private func runFetchScript() -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: fetchCommand.executable)
        process.arguments = fetchCommand.arguments
        let outPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = Pipe()

        do {
            try process.run()
        } catch {
            return "failed"
        }

        let timeoutItem = DispatchWorkItem {
            if process.isRunning { process.terminate() }
        }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + fetchTimeout, execute: timeoutItem)
        let data = outPipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        timeoutItem.cancel()

        // The timeout's terminate() is the ONLY signal source (the script exits normally otherwise),
        // so an uncaughtSignal means we killed it mid-run: its output is truncated and the status
        // lines are partial — report incomplete rather than passing a partial summary off as complete.
        // A normal exit (status 0 = some ok, 1 = all failed) has complete lines, so those parse fine.
        if process.terminationReason == .uncaughtSignal {
            return "timed out"
        }

        // Each stdout line is "<name>: <status>"; condense to "<name> <status> · …".
        let parts = String(decoding: data, as: UTF8.self)
            .split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
            .map { $0.replacingOccurrences(of: ": ", with: " ") }
        return parts.isEmpty ? "failed" : parts.joined(separator: " · ")
    }

    private func fetchSnapshot() -> Result<[MeterRow], Error> {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: snapshotCommand.executable)
        process.arguments = snapshotCommand.arguments
        let outPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = Pipe()

        do {
            try process.run()
        } catch {
            return .failure(error)
        }

        let timeoutItem = DispatchWorkItem {
            if process.isRunning { process.terminate() }
        }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + processTimeout, execute: timeoutItem)
        process.waitUntilExit()
        timeoutItem.cancel()

        let data = outPipe.fileHandleForReading.readDataToEndOfFile()
        guard process.terminationStatus == 0, !data.isEmpty else {
            return .failure(NSError(domain: "UsageHUD", code: Int(process.terminationStatus)))
        }

        do {
            let rows = try JSONDecoder().decode([MeterRow].self, from: data)
            return .success(rows)
        } catch {
            return .failure(error)
        }
    }
}

/// The oldest CONFIRMATION among the rows this app can keep current — "updated HH:MM" describes the
/// weakest of those, since taking the newest would let one just-refreshed row vouch for stale
/// siblings. Rows the unified fetch cannot refresh are excluded. A snapshot where no row claims
/// `canFetch` (older data script) falls back
/// to all rows. Reads `seenAt`, not `asOf`: a row confirmed seconds ago whose value last moved days
/// ago is current, and reporting its `asOf` would be a false staleness alarm.
func oldestDataDate(in rows: [MeterRow]) -> Date? {
    let fetchable = rows.filter { $0.canFetch == true }
    let scope = fetchable.isEmpty ? rows : fetchable
    return scope.compactMap(\.seenAt).min().map(Date.init(timeIntervalSince1970:))
}
