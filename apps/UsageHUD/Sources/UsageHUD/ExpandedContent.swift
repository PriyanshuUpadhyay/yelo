import AppKit
import SwiftUI


/// Calm bars carry provider identity; severity overrides to amber/red.
private func providerColor(_ provider: String) -> Color {
    provider == "codex" ? codexColor : claudeColor
}

private func providerLightColor(_ provider: String) -> Color {
    provider == "codex" ? codexLightColor : claudeLightColor
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
    let fetchClause = row.canFetch == false
        ? " · use this account in the CLI to update it"
        : " · click Refresh to fetch current usage"
    if row.isStale, let seenAt = row.seenAt {
        let age = ageLabel(since: Date(timeIntervalSince1970: seenAt), now: now)
        return "\(sourceLabel(row.source)) last confirmed \(age)\(fetchClause)"
    }
    if row.state == "missing" {
        return "No local sample for this window\(fetchClause)"
    }
    return nil
}

/// Subtitle under an account name. A failed fetch leads and outranks the last-used mark: it is
/// the one thing the person can act on. Age is the oldest confirmed sample across the account's rows.
func accountSubtitle(_ rows: [MeterRow], fetchStatus: String?, now: Date = Date()) -> (text: String, warning: Bool) {
    let age = rows.compactMap(\.seenAt).min().map { ageLabel(since: Date(timeIntervalSince1970: $0), now: now) }
    if fetchStatus == "fetch-failed" { return ("Fetch failed" + (age.map { " · \($0)" } ?? ""), true) }
    if fetchStatus == "auth-stale" { return ("Sign in needed" + (age.map { " · \($0)" } ?? ""), true) }
    let parts = [age, rows.contains { $0.active == true } ? "last used" : nil].compactMap { $0 }
    return (parts.joined(separator: " · "), false)
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
            .scaleEffect(configuration.isPressed ? 0.94 : 1)
            .animation(.easeOut(duration: 0.1), value: configuration.isPressed)
    }
}

private struct UsageBarView: View {
    let pct: Int
    let start: Color
    let end: Color
    /// Stale values stay readable but must not look as loud as a confirmed one.
    let dimmed: Bool
    let reduceMotion: Bool
    let rowIndex: Int

    @State private var grown = false

    var body: some View {
        GeometryReader { proxy in
            let target = proxy.size.width * CGFloat(min(max(pct, 0), 100)) / 100
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.08))
                Capsule()
                    .fill(LinearGradient(colors: [start, end], startPoint: .leading, endPoint: .trailing))
                    .shadow(color: end.opacity(0.55), radius: 4)
                    .opacity(dimmed ? 0.45 : 1)
                    .frame(width: grown ? target : 0)
                    // The first fill is animated by the explicit `grown` transaction below; later
                    // value changes come through here.
                    .animation(reduceMotion ? nil : .spring(response: 0.5, dampingFraction: 1), value: target)
            }
        }
        .frame(height: 6)
        .accessibilityHidden(true)
        .onAppear {
            guard !reduceMotion else { grown = true; return }
            withAnimation(.spring(response: 0.6, dampingFraction: 1).delay(0.03 * Double(rowIndex))) {
                grown = true
            }
        }
    }
}

private struct MeterCell: View {
    let row: MeterRow?
    let pressure: RowPressure?
    let fetchedAt: Date?
    let reduceMotion: Bool
    /// Position in the whole account list — the bars fill in a short cascade rather than all at once.
    let rowIndex: Int

    private var severity: PresentationSeverity {
        PresentationSeverity(pct: row?.pct ?? 0, pressure: pressure?.pressure)
    }

    private var isRisk: Bool { row?.isLive == true && riskText(pressure) != nil }

    private var detail: String {
        guard let row else { return "" }
        if row.isLive, let risk = riskText(pressure) { return risk }
        return resetText(row, fetchedAt: fetchedAt) ?? ""
    }

    private var barColors: (start: Color, end: Color) {
        guard let row else { return (claudeColor, claudeLightColor) }
        guard row.isLive else { return (providerColor(row.provider), providerLightColor(row.provider)) }
        switch severity {
        case .danger: return (dangerColor, dangerLightColor)
        case .warn: return (warnColor, warnLightColor)
        case .calm: return (providerColor(row.provider), providerLightColor(row.provider))
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let row, let pct = row.pct {
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Text("\(pct)%")
                        .font(.system(size: 17, weight: .semibold, design: .rounded))
                        .monospacedDigit()
                        .contentTransition(.numericText())
                        .animation(reduceMotion ? nil : .spring(response: 0.4, dampingFraction: 1), value: pct)
                        .foregroundStyle(row.isLive ? severity.textColor ?? textPrimary : textPrimary.opacity(0.8))
                    if row.isStale {
                        Image(systemName: "clock")
                            .font(.system(size: 8, weight: .medium))
                            .foregroundStyle(textTertiary)
                            .accessibilityLabel("Older sample")
                    }
                }
                UsageBarView(pct: pct, start: barColors.start, end: barColors.end,
                             dimmed: !row.isLive, reduceMotion: reduceMotion, rowIndex: rowIndex)
                Text(detail)
                    .font(.system(size: 10, weight: isRisk ? .medium : .regular))
                    .foregroundStyle(isRisk ? severity.textColor ?? textSecondary : textSecondary)
                    .lineLimit(1)
            } else {
                Text("—").font(.system(size: 17, design: .rounded)).foregroundStyle(textSecondary)
                Text("No sample").font(.system(size: 10)).foregroundStyle(textSecondary)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 45, alignment: .leading)
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
    let fetchStatuses: [String: String]
    /// Index of this card's first account within the whole list, so the bar cascade runs top to
    /// bottom across cards instead of restarting per provider.
    let rowIndexOffset: Int

    @State private var hoveredLabel: String?

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
                    Circle().fill(providerColor(provider)).frame(width: 8, height: 8)
                        .shadow(color: providerColor(provider).opacity(0.7), radius: 5)
                        .accessibilityHidden(true)
                    Text(provider == "codex" ? "Codex" : "Claude")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(textPrimary)
                }
                .frame(width: accountWidth, alignment: .leading)
                ForEach(windows, id: \.self) { window in
                    Text((rows.first { $0.window == window }.map(windowLabel) ?? window).uppercased())
                        .font(.system(size: 10, weight: .medium))
                        .tracking(0.6)
                        .foregroundStyle(textSecondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if windows.isEmpty { Spacer(minLength: 0) }
            }
            .padding(.bottom, 8)

            ForEach(Array(accounts.enumerated()), id: \.element.label) { index, account in
                let subtitle = accountSubtitle(account.rows, fetchStatus: fetchStatuses[account.label])
                HStack(spacing: 18) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(accountName(label: account.label, provider: provider))
                            .font(.system(size: 12.5, weight: .medium))
                            .foregroundStyle(textPrimary)
                            .lineLimit(1).truncationMode(.middle)
                            .help(accountName(label: account.label, provider: provider))
                        Text(subtitle.text)
                            .font(.system(size: 10))
                            .foregroundStyle(subtitle.warning ? warnTextColor : textSecondary)
                            .lineLimit(1)
                            .help(subtitle.warning
                                  ? "Refresh could not update this account. The value shown is the last local sample."
                                  : "When this account's usage was last confirmed")
                    }
                    .frame(width: accountWidth, alignment: .leading)
                    if let unavailable = account.rows.first(where: { $0.state == "offline" || $0.state == "logged_out" }) {
                        Text(availabilityCopy(unavailable))
                            .font(.system(size: 11)).foregroundStyle(textSecondary)
                            .frame(maxWidth: .infinity, minHeight: 45, alignment: .leading)
                            .fixedSize(horizontal: false, vertical: true)
                    } else {
                        ForEach(windows, id: \.self) { window in
                            MeterCell(
                                row: account.rows.first { $0.window == window },
                                pressure: pressures.first { $0.label == account.label && $0.window == window },
                                fetchedAt: fetchedAt, reduceMotion: reduceMotion,
                                rowIndex: rowIndexOffset + index
                            )
                        }
                    }
                }
                .padding(.vertical, 5)
                .background(
                    RoundedRectangle(cornerRadius: 10)
                        .fill(Color.white.opacity(hoveredLabel == account.label ? 0.03 : 0))
                )
                .onHover { inside in
                    withAnimation(.easeOut(duration: 0.12)) {
                        hoveredLabel = inside ? account.label : (hoveredLabel == account.label ? nil : hoveredLabel)
                    }
                }
                .overlay(alignment: .top) {
                    if index > 0 {
                        Rectangle()
                            .fill(contrast == .increased ? Color.white.opacity(0.4) : hairlineColor)
                            .frame(height: 0.5)
                            .padding(.horizontal, 8)
                    }
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 16).fill(cardInsetColor))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(hairlineColor, lineWidth: 1))
    }
}

struct ExpandedContent: View {
    @ObservedObject var model: UsageModel
    let reduceMotion: Bool
    @State private var naturalHeight: CGFloat = 0
    @State private var refreshBounce = 0

    // Keep controls visible while only the account list scrolls.
    private let headerHeight: CGFloat = 72
    private let footerHeight: CGFloat = 40
    private var availableHeight: CGFloat { min(640, max(0, hudPanelSize.height - model.notchTopInset)) }
    private var visibleHeight: CGFloat { min(naturalHeight > 0 ? naturalHeight + headerHeight + footerHeight : availableHeight, availableHeight) }
    private var accountCount: Int { groupedByLabel(model.rows).count }
    private var hasOlderSamples: Bool { model.rows.contains(where: \.isStale) }
    private var isBusy: Bool { model.isFetching || model.isRefreshing }

    private var mood: Mood {
        usageMood(hasLoadedSnapshot: model.hasLoadedSnapshot, isFetching: model.isFetching,
                  rows: model.rows, pressures: model.rowPressures)
    }

    private var providerSections: [(provider: String, rows: [MeterRow])] {
        ["claude", "codex"].compactMap { provider in
            let rows = model.rows.filter { $0.provider == provider }
            return rows.isEmpty ? nil : (provider, rows)
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            header.frame(height: headerHeight)
            ScrollView(.vertical) {
                VStack(alignment: .leading, spacing: 14) {
                    if model.firstLaunchFailure {
                        emptyState(symbol: "exclamationmark.circle", title: "Usage is unavailable",
                                   detail: "Check that Jello is installed, then click Refresh.")
                    } else if model.rows.isEmpty && !model.hasLoadedSnapshot {
                        HStack(spacing: 10) {
                            ProgressView().controlSize(.small)
                            Text("Reading usage…").font(.system(size: 12)).foregroundStyle(textSecondary)
                        }
                        .frame(maxWidth: .infinity, minHeight: 150)
                    } else if model.rows.isEmpty {
                        emptyState(symbol: "person.crop.circle.badge.plus", title: "No accounts yet",
                                   detail: "Add a Claude or Codex profile with Jello.\nUsage appears after you use it.")
                    } else {
                        let sections = providerSections
                        ForEach(Array(sections.enumerated()), id: \.element.provider) { index, section in
                            ProviderSection(
                                provider: section.provider, rows: section.rows, pressures: model.rowPressures,
                                fetchedAt: model.lastSnapshotAt, reduceMotion: reduceMotion,
                                fetchStatuses: model.apiFetchResult?.statuses ?? [:],
                                rowIndexOffset: sections[..<index].reduce(0) { $0 + groupedByLabel($1.rows).count }
                            )
                        }
                    }
                }
                .padding(.vertical, 6)
                .frame(width: expandedSurfaceWidth - 56)
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
        .padding(.horizontal, 28)
        .frame(width: expandedSurfaceWidth, height: visibleHeight, alignment: .top)
    }

    private var header: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Usage").font(.system(size: 17, weight: .semibold)).foregroundStyle(textPrimary)
                HStack(spacing: 5) {
                    Circle().fill(mood.dotColor).frame(width: 6, height: 6)
                        .accessibilityHidden(true)
                    Text("\(accountCount == 0 ? "Claude & Codex" : "\(accountCount) \(accountCount == 1 ? "account" : "accounts")") · \(mood.caption)")
                        .font(.system(size: 11)).foregroundStyle(textSecondary)
                }
            }
            Spacer(minLength: 4)
            Button(action: {
                refreshBounce += 1
                model.fetchFromAPI()
            }) {
                // Spinning the glyph in place keeps the header from reflowing mid-fetch, which a
                // swapped-in ProgressView used to cause.
                Image(systemName: "arrow.clockwise")
                    .symbolEffect(.bounce, value: refreshBounce)
                    .rotationEffect(.degrees(isBusy && !reduceMotion ? 360 : 0))
                    .animation(isBusy && !reduceMotion
                               ? .linear(duration: 0.9).repeatForever(autoreverses: false)
                               : .easeOut(duration: 0.2),
                               value: isBusy)
            }
            .buttonStyle(HUDButtonStyle())
            .disabled(isBusy)
            .help("Refresh usage from API (⌘R). Background reads stay local.")
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
        HStack(spacing: 8) {
            Image(systemName: model.fetchFailed || model.apiFetchResult?.warning == true ? "exclamationmark.circle" : hasOlderSamples ? "clock" : "internaldrive")
                .font(.system(size: 11))
            Text(footerMessage)
                .font(.system(size: 11, weight: .medium))
                .lineLimit(1)
            Spacer(minLength: 0)
        }
        .foregroundStyle(model.fetchFailed || model.apiFetchResult?.warning == true ? warnTextColor : textSecondary)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(alignment: .top) { hairlineColor.frame(height: 0.5).offset(y: -6) }
    }

    private var footerMessage: String {
        if model.isFetching { return "Fetching usage from API…" }
        if model.fetchFailed { return "Could not read local usage. Try Refresh." }
        if let result = model.apiFetchResult { return "Last fetch: \(result.message)" }
        return hasOlderSamples ? "Clock marks an older sample." : "Usage from local samples"
    }

    private func emptyState(symbol: String, title: String, detail: String) -> some View {
        VStack(spacing: 10) {
            Image(systemName: symbol).font(.system(size: 26, weight: .light)).foregroundStyle(textSecondary)
            Text(title).font(.system(size: 14, weight: .medium)).foregroundStyle(textPrimary)
            Text(detail).font(.system(size: 12)).foregroundStyle(textSecondary)
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
