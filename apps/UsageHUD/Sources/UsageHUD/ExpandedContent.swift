import AppKit
import SwiftUI


/// Calm bars carry provider identity; severity overrides to amber/red.
private func providerColor(_ provider: String) -> Color {
    provider == "codex" ? codexColor : claudeColor
}

private let timeFormatter: DateFormatter = {
    let formatter = DateFormatter()
    formatter.dateFormat = "HH:mm"
    return formatter
}()

private let weekdayTimeFormatter: DateFormatter = {
    let formatter = DateFormatter()
    formatter.dateFormat = "EEE HH:mm"
    return formatter
}()

/// Red-pressure ETAs can land past midnight (weekly window: days out), where a bare HH:mm silently
/// reads as today — prefix the weekday whenever the ETA isn't today.
func limitTimeLabel(_ date: Date, now: Date = Date(), calendar: Calendar = .current) -> String {
    let formatter = calendar.isDate(date, inSameDayAs: now) ? timeFormatter : weekdayTimeFormatter
    return formatter.string(from: date)
}

private func availabilityCopy(_ row: MeterRow) -> String {
    row.state == "logged_out" ? "Sign in to this account to see usage." : "No local usage yet. Use this account in the CLI to update it."
}

private func windowOrder(_ window: String?) -> Int {
    switch window {
    case "5h": return 0
    case "7d": return 1
    default: return 2
    }
}

private func windowLabel(_ row: MeterRow) -> String {
    switch row.window {
    case "5h": return "5 hours"
    case "7d": return "Weekly"
    case "fb": return "Fable · week"
    case let window?: return window.isEmpty ? "—" : window
    case nil: return "—"
    }
}

private func accountName(label: String, provider: String) -> String {
    let prefix = provider == "codex" ? "cx·" : "cl·"
    return label.hasPrefix(prefix) ? String(label.dropFirst(prefix.count)) : (label == "cx" ? "Default" : label)
}

private func sourceLabel(_ source: String?) -> String {
    switch source {
    case "api": return "API"
    case "statusline": return "Claude session"
    case "rollout": return "Codex session"
    default: return "usage source"
    }
}

private func ageLabel(since date: Date, now: Date) -> String {
    let seconds = max(0, now.timeIntervalSince(date))
    if seconds < 60 { return "just now" }
    if seconds < 3600 { return "\(max(1, Int(seconds / 60)))m ago" }
    if seconds < 86_400 { return "\(max(1, Int(seconds / 3600)))h ago" }
    return "\(max(1, Int(seconds / 86_400)))d ago"
}

/// Stale values explain their age and the action that can produce a new local sample.
func freshnessDetail(_ row: MeterRow, now: Date = Date()) -> String? {
    guard !row.isLive else { return nil }
    if row.isStale, row.reset == "now" {
        return "Window reset · waiting for a current \(sourceLabel(row.source)) sample"
    }
    let fetchClause = " · click Refresh to fetch current usage"
    if row.isStale, let seenAt = row.seenAt {
        let age = ageLabel(since: Date(timeIntervalSince1970: seenAt), now: now)
        return "\(sourceLabel(row.source)) last confirmed \(age)\(fetchClause)"
    }
    if row.state == "missing" {
        return "No local sample for this window\(fetchClause)"
    }
    return nil
}

/// Risk meta for one row: red → limit ETA, amber → projected pct, green/no-trend → nil.
private func riskText(_ pressure: RowPressure?) -> String? {
    guard let pressure, pressure.pressure != .green else { return nil }
    if pressure.pressure == .red, let eta = pressure.eta100 {
        return "limit ~\(limitTimeLabel(Date().addingTimeInterval(eta * 3600)))"
    }
    guard let projected = pressure.projected else { return nil }
    return "→ \(min(100, Int(projected.rounded())))%"
}

/// "resets 14:30" — data-script resets are relative ("4h37m") and humanized from the absolute
/// reset epoch at fetch time (parse_cache runs humanize_until when the HUD polls, not at
/// cache-write), so the conversion anchors to the fetch that produced this row (a retained
/// snapshot after a failed refresh must not drift the wall time forward); raw-string fallback
/// when unanchorable.
private func resetText(_ row: MeterRow, fetchedAt: Date?) -> String? {
    guard let reset = row.reset else { return nil }
    // An expired sample has no upcoming reset. Its clock mark explains the state.
    if reset == "now" { return row.isStale ? nil : "Resets now" }
    if let hours = parseResetHours(reset), let fetchedAt {
        return "Resets \(limitTimeLabel(fetchedAt.addingTimeInterval(hours * 3600)))"
    }
    return "Resets in \(reset)"
}

private func groupedByLabel(_ rows: [MeterRow]) -> [(label: String, rows: [MeterRow])] {
    var seen = Set<String>()
    var result: [(label: String, rows: [MeterRow])] = []
    for row in rows where seen.insert(row.label).inserted {
        result.append((row.label, rows.filter { $0.label == row.label }))
    }
    return result
}

private struct HUDButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .medium))
            .frame(width: 30, height: 30)
            .background(.primary.opacity(configuration.isPressed ? 0.16 : 0.06), in: Circle())
            .contentShape(Circle())
    }
}

private struct UsageBarView: View {
    let pct: Int
    let fill: Color
    let reduceMotion: Bool

    var body: some View {
        GeometryReader { proxy in
            let fillWidth = proxy.size.width * CGFloat(min(max(pct, 0), 100)) / 100
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.09))
                Capsule().fill(fill).frame(width: fillWidth)
                    .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: fillWidth)
            }
        }
        .frame(height: 3)
        .accessibilityHidden(true)
    }
}

private struct MeterCell: View {
    let row: MeterRow?
    let pressure: RowPressure?
    let fetchedAt: Date?
    let reduceMotion: Bool

    private var severity: PresentationSeverity {
        PresentationSeverity(pct: row?.pct ?? 0, pressure: pressure?.pressure)
    }

    private var detail: String {
        guard let row else { return "" }
        if row.isLive, let risk = riskText(pressure) { return risk }
        return resetText(row, fetchedAt: fetchedAt) ?? ""
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let row, let pct = row.pct {
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Text("\(pct)%")
                        .font(.system(size: 16, weight: .medium, design: .rounded))
                        .monospacedDigit()
                        .foregroundStyle(row.isLive ? severity.textColor ?? .primary : .primary.opacity(0.8))
                    if row.isStale {
                        Image(systemName: "clock")
                            .font(.system(size: 9, weight: .medium))
                            .foregroundStyle(.secondary)
                            .accessibilityLabel("Older sample")
                    }
                }
                UsageBarView(
                    pct: pct,
                    fill: row.isLive ? severity.barColor ?? providerColor(row.provider) : providerColor(row.provider).opacity(0.45),
                    reduceMotion: reduceMotion
                )
                Text(detail)
                    .font(.system(size: 10))
                    .foregroundStyle(row.isLive ? severity.textColor ?? .secondary : .secondary)
                    .lineLimit(1)
            } else {
                Text("—").font(.system(size: 16)).foregroundStyle(.secondary)
                Text("No sample").font(.system(size: 10)).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(row.map { "\(windowLabel($0)), \($0.pct.map { "\($0) percent used" } ?? "No sample")" } ?? "No sample")
        .accessibilityValue([detail, row.flatMap { freshnessDetail($0) } ?? ""].filter { !$0.isEmpty }.joined(separator: ". "))
        .help(row.map { freshnessDetail($0) ?? windowLabel($0) } ?? "No local sample for this window")
    }
}

private struct ProviderSection: View {
    @Environment(\.colorSchemeContrast) private var contrast
    let provider: String
    let rows: [MeterRow]
    let pressures: [RowPressure]
    let fetchedAt: Date?
    let reduceMotion: Bool

    private let accountWidth: CGFloat = 136
    private var accounts: [(label: String, rows: [MeterRow])] { groupedByLabel(rows) }
    private var windows: [String] {
        Set(rows.compactMap(\.window)).sorted {
            windowOrder($0) == windowOrder($1) ? $0 < $1 : windowOrder($0) < windowOrder($1)
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 18) {
                HStack(spacing: 7) {
                    Circle().fill(providerColor(provider)).frame(width: 6, height: 6)
                        .accessibilityHidden(true)
                    Text(provider == "codex" ? "Codex" : "Claude")
                        .font(.system(size: 13, weight: .semibold))
                }
                .frame(width: accountWidth, alignment: .leading)
                ForEach(windows, id: \.self) { window in
                    Text(rows.first { $0.window == window }.map(windowLabel) ?? window)
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if windows.isEmpty { Spacer(minLength: 0) }
            }
            .padding(.bottom, 10)

            ForEach(accounts, id: \.label) { account in
                HStack(spacing: 18) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(accountName(label: account.label, provider: provider))
                            .font(.system(size: 12, weight: .medium))
                            .lineLimit(1).truncationMode(.middle)
                            .help(accountName(label: account.label, provider: provider))
                        Text(account.rows.contains { $0.active == true } ? "Last used" : accountAge(account.rows))
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    .frame(width: accountWidth, alignment: .leading)
                    if let unavailable = account.rows.first(where: { $0.state == "offline" || $0.state == "logged_out" }) {
                        Text(availabilityCopy(unavailable))
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading)
                            .fixedSize(horizontal: false, vertical: true)
                    } else {
                        ForEach(windows, id: \.self) { window in
                            MeterCell(
                                row: account.rows.first { $0.window == window },
                                pressure: pressures.first { $0.label == account.label && $0.window == window },
                                fetchedAt: fetchedAt, reduceMotion: reduceMotion
                            )
                        }
                    }
                }
                .padding(.vertical, 6)
                .overlay(alignment: .top) {
                    Rectangle().fill(Color.primary.opacity(contrast == .increased ? 0.4 : 0.08)).frame(height: 0.5)
                }
            }
        }
    }

    private func accountAge(_ rows: [MeterRow]) -> String {
        guard let seen = rows.compactMap(\.seenAt).min() else { return "" }
        return ageLabel(since: Date(timeIntervalSince1970: seen), now: Date())
    }
}

struct ExpandedContent: View {
    @ObservedObject var model: UsageModel
    let reduceMotion: Bool
    @State private var naturalHeight: CGFloat = 0

    // Keep controls visible while only the account list scrolls.
    private let headerHeight: CGFloat = 66
    private let footerHeight: CGFloat = 58
    private var availableHeight: CGFloat { min(640, max(0, hudPanelSize.height - model.notchTopInset)) }
    private var visibleHeight: CGFloat { min(naturalHeight > 0 ? naturalHeight + headerHeight + footerHeight : availableHeight, availableHeight) }
    private var accountCount: Int { groupedByLabel(model.rows).count }
    private var hasOlderSamples: Bool { model.rows.contains(where: \.isStale) }

    var body: some View {
        VStack(spacing: 0) {
            header.frame(height: headerHeight)
            ScrollView(.vertical) {
                VStack(alignment: .leading, spacing: 20) {
                    if model.firstLaunchFailure {
                        emptyState(symbol: "exclamationmark.circle", title: "Usage is unavailable",
                                   detail: "Check that Jello is installed, then click Refresh.")
                    } else if model.rows.isEmpty && !model.hasLoadedSnapshot {
                        HStack(spacing: 10) {
                            ProgressView().controlSize(.small)
                            Text("Reading usage…").font(.system(size: 12)).foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity, minHeight: 150)
                    } else if model.rows.isEmpty {
                        emptyState(symbol: "person.crop.circle.badge.plus", title: "No accounts yet",
                                   detail: "Add a Claude or Codex profile with Jello.\nUsage appears after you use it.")
                    } else {
                        ForEach(["claude", "codex"], id: \.self) { provider in
                            let rows = model.rows.filter { $0.provider == provider }
                            if !rows.isEmpty {
                                ProviderSection(provider: provider, rows: rows, pressures: model.rowPressures,
                                                fetchedAt: model.lastSnapshotAt, reduceMotion: reduceMotion)
                            }
                        }
                    }
                }
                .padding(.vertical, 8)
                .frame(width: expandedSurfaceWidth - 68)
                .background(GeometryReader { proxy in
                    Color.clear
                        .onAppear { reportMeasuredSize(proxy.size) }
                        .onChange(of: proxy.size) { _, size in reportMeasuredSize(size) }
                })
            }
            .scrollBounceBehavior(.basedOnSize)
            .frame(height: max(0, visibleHeight - headerHeight - footerHeight))
            footer.frame(height: footerHeight)
        }
        .padding(.horizontal, 34)
        .frame(width: expandedSurfaceWidth, height: visibleHeight, alignment: .top)
    }

    private var header: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Usage").font(.system(size: 18, weight: .semibold))
                Text(accountCount == 0 ? "Claude & Codex" : "\(accountCount) \(accountCount == 1 ? "account" : "accounts") · Percentage used")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Spacer(minLength: 4)
            Button(action: model.fetchFromAPI) {
                if model.isFetching || model.isRefreshing {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: "arrow.clockwise")
                }
            }
            .buttonStyle(HUDButtonStyle())
            .disabled(model.isFetching || model.isRefreshing)
            .help("Refresh usage from API (⌘R)")
            .accessibilityLabel("Refresh usage from API")
            .keyboardShortcut("r", modifiers: .command)
            Button {
                model.openedFromMenu = false
                model.isCollapsed = true
            } label: {
                Image(systemName: "xmark")
            }
            .buttonStyle(HUDButtonStyle())
            .help("Close usage (Esc)")
            .accessibilityLabel("Close usage")
            .keyboardShortcut(.cancelAction)
        }
    }

    private var footer: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: model.fetchFailed || model.apiFetchResult?.warning == true ? "exclamationmark.circle" : hasOlderSamples ? "clock" : "internaldrive")
                .font(.system(size: 11))
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 4) {
                Text(footerMessage)
                    .font(.system(size: 11, weight: .medium))
                Text("Refresh fetches from API. Background reads stay local.")
                    .font(.system(size: 10))
            }
            Spacer(minLength: 0)
        }
        .foregroundStyle(model.fetchFailed || model.apiFetchResult?.warning == true ? warnTextColor : .secondary)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(alignment: .top) { Color.primary.opacity(0.08).frame(height: 0.5).offset(y: -10) }
    }

    private var footerMessage: String {
        if model.isFetching { return "Fetching usage from API…" }
        if model.fetchFailed { return "Could not read local usage. Try Refresh." }
        if let result = model.apiFetchResult { return "Last fetch: \(result.message)" }
        return hasOlderSamples ? "Clock marks an older sample." : "Usage from local samples"
    }

    private func emptyState(symbol: String, title: String, detail: String) -> some View {
        VStack(spacing: 10) {
            Image(systemName: symbol).font(.system(size: 26, weight: .light)).foregroundStyle(.secondary)
            Text(title).font(.system(size: 14, weight: .medium))
            Text(detail).font(.system(size: 12)).foregroundStyle(.secondary)
                .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 42)
        .frame(maxWidth: .infinity, minHeight: 150)
    }

    private func reportMeasuredSize(_ size: CGSize) {
        naturalHeight = size.height
        model.expandedContentHeight = min(size.height + headerHeight + footerHeight, availableHeight)
        model.expandedContentWidth = expandedSurfaceWidth
    }
}
