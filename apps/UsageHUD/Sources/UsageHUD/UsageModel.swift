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
    /// Whether the CLI can fetch this provider's usage on request.
    var canFetch: Bool? = nil
}

extension MeterRow {
    /// The data script gates each row on the age of its own source data. `ok` means the percentage
    /// describes the account right now; `stale` may carry the last confirmed percentage for display,
    /// while `missing` has no value. Never feed a non-live row into history, pressure, or status.
    var isLive: Bool { state == "ok" }
    /// Data exists but is too old to present as current.
    var isStale: Bool { state == "stale" }
}

/// Automatic reads stay local. API fetch is a separate user action.
func snapshotArguments(
    environment: [String: String]
) -> (executable: String, arguments: [String]) {
    if let script = environment["USAGE_HUD_SCRIPT"] {
        return (script, ["--json"])
    }
    return (yeloPath(environment: environment), ["usage", "show", "--json"])
}

private func yeloPath(environment: [String: String]) -> String {
    environment["YELO_BIN"] ?? ("~/.local/bin/yelo" as NSString).expandingTildeInPath
}

/// Polls local usage files through the CLI. This path reads no account credentials.
final class UsageModel: ObservableObject {
    @Published var rows: [MeterRow] = []
    /// Oldest source-data timestamp in the accepted snapshot.
    @Published var lastFetchAt: Date?
    /// When the local data snapshot was read; reset strings are relative to this moment.
    @Published var lastSnapshotAt: Date?
    @Published var fetchFailed: Bool = false
    @Published var hasLoadedSnapshot: Bool = false
    @Published var isRefreshing: Bool = false
    @Published var isFetching: Bool = false
    @Published var apiFetchResult: APIFetchResult?

    /// No rows AND a failed fetch = the FIRST-EVER fetch failed (never had data to show) — distinct
    /// from a mid-run failure, which keeps last-known rows on screen.
    var firstLaunchFailure: Bool { rows.isEmpty && fetchFailed }

    /// Hover opens temporarily; opening from the menu keeps the panel open until dismissal.
    @Published var isCollapsed: Bool = true
    @Published var openedFromMenu: Bool = false

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

    /// Trend history and pressure update after a successful local read.
    @Published var history: [HistorySample] = []
    @Published var rowPressures: [RowPressure] = []


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

    private let historyStore: HistoryStore

    private var timer: Timer?
    // Queue one reread if a request arrives while the previous read is still running.
    private var refreshQueued = false
    private let snapshotCommand: (executable: String, arguments: [String])
    private let fetchExecutable: String
    private let pollInterval: TimeInterval = 120
    // The script is local-only (cache reads + rollout scans, no network) — 8s is generous headroom.
    private let processTimeout: TimeInterval = 8

    init(environment: [String: String] = ProcessInfo.processInfo.environment,
         historyStore: HistoryStore = HistoryStore()) {
        self.historyStore = historyStore
        snapshotCommand = snapshotArguments(environment: environment)
        fetchExecutable = yeloPath(environment: environment)
    }

    func fetchFromAPI() {
        guard !isFetching else { return }
        isFetching = true
        apiFetchResult = nil
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let result = runAPIFetch(executable: self.fetchExecutable)
            DispatchQueue.main.async {
                self.apiFetchResult = result
                self.isFetching = false
                // Read again even after partial failure: successful accounts already wrote caches.
                self.refresh()
            }
        }
    }

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
                    self.hasLoadedSnapshot = true
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

    private func fetchSnapshot() -> Result<[MeterRow], Error> {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: snapshotCommand.executable)
        process.arguments = snapshotCommand.arguments
        let outPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = FileHandle.nullDevice

        do {
            try process.run()
        } catch {
            return .failure(error)
        }

        let timeoutItem = DispatchWorkItem {
            if process.isRunning { process.terminate() }
        }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + processTimeout, execute: timeoutItem)
        // Drain while the child runs so a large account list cannot fill the pipe.
        let data = outPipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        timeoutItem.cancel()
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

/// Use the oldest confirmation so a fresh account cannot hide another account's stale data.
/// `asOf` records value changes and is not a freshness timestamp.
func oldestDataDate(in rows: [MeterRow]) -> Date? {
    return rows.compactMap(\.seenAt).min().map(Date.init(timeIntervalSince1970:))
}

struct APIFetchResult {
    let message: String
    let warning: Bool
    /// Per-account outcome word, keyed by the same `cl·NAME` / `cx·NAME` labels the rows carry.
    var statuses: [String: String] = [:]
}

/// The CLI exits zero when at least one account succeeds. Check every status before claiming success.
/// Each line is `label: status`; the label can hold a `: ` of its own, so the LAST one splits.
func apiFetchSummary(_ output: String, exitCode: Int32) -> APIFetchResult {
    let parsed: [(String, String)] = output.split(separator: "\n").compactMap { line in
        guard let separator = line.range(of: ": ", options: .backwards) else { return nil }
        return (String(line[..<separator.lowerBound]), String(line[separator.upperBound...]))
    }
    let statuses = Dictionary(parsed, uniquingKeysWith: { _, last in last })
    let words = parsed.map(\.1)
    let updated = words.filter { $0 == "ok" }.count
    guard !words.isEmpty, exitCode == 0 || exitCode == 1 else {
        return APIFetchResult(message: "Could not fetch usage. Try Refresh again.", warning: true)
    }
    if exitCode == 0 && updated == words.count {
        return APIFetchResult(message: "Updated \(updated) \(updated == 1 ? "account" : "accounts").",
                              warning: false, statuses: statuses)
    }
    if words.contains("auth-stale") {
        return APIFetchResult(
            message: "Updated \(updated) of \(words.count) accounts. Sign in to marked accounts with their launchers.",
            warning: true, statuses: statuses)
    }
    if words.allSatisfy({ $0 == "ok" || $0 == "fable-write-failed" }) {
        return APIFetchResult(message: "Usage updated, but a Fable sample could not be saved.",
                              warning: true, statuses: statuses)
    }
    // No "Try Refresh again": the person just pressed Refresh, and the rows name what failed.
    return APIFetchResult(message: "Updated \(updated) of \(words.count) accounts.",
                          warning: true, statuses: statuses)
}

func runAPIFetch(executable: String, timeout: TimeInterval = 120) -> APIFetchResult {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = ["usage", "fetch"]
    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = FileHandle.nullDevice
    do {
        try process.run()
    } catch {
        return APIFetchResult(message: "Could not start Yelo. Check its installation.", warning: true)
    }
    let deadline = DispatchWorkItem {
        if process.isRunning { process.terminate() }
    }
    DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + timeout, execute: deadline)
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    deadline.cancel()
    guard process.terminationReason == .exit else {
        return APIFetchResult(message: "API fetch did not finish. Try Refresh again.", warning: true)
    }
    return apiFetchSummary(String(decoding: data, as: UTF8.self), exitCode: process.terminationStatus)
}
