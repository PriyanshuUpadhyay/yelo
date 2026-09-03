import AppKit
import SwiftUI

private let barFillFade = 0.5
private let hairline = Color.primary.opacity(0.12)

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
    if row.state == "logged_out" {
        if row.provider == "codex", row.primeSignedIn == true {
            return "Codex logged out · Prime Agent signed in"
        }
        return "logged out"
    }
    let reason = row.reason ?? ""
    return reason.isEmpty || reason == "no data" ? "offline · no recent data" : "offline · \(reason.lowercased())"
}

private func primeStatusCopy(_ rows: [MeterRow]) -> String? {
    guard rows.first?.provider == "codex", let state = rows.compactMap(\.primeSignedIn).first else {
        return nil
    }
    return state ? "Prime Agent signed in" : "Prime Agent logged out"
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
    case "5h": return "5-hour session"
    case "7d": return row.provider == "codex" ? "Weekly" : "7-day · all models"
    case "fb": return "Weekly · Fable"
    case let window?: return window.isEmpty ? "—" : window
    case nil: return "—"
    }
}

private func sectionTitle(label: String, provider: String) -> String {
    let (title, prefix, bare) = provider == "codex" ? ("CODEX", "cx·", "cx") : ("CLAUDE", "cl·", "cl")
    let name = label.hasPrefix(prefix) ? String(label.dropFirst(prefix.count)) : (label == bare ? "" : label)
    return name.isEmpty ? title : "\(title) · \(name.uppercased())"
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

/// Only a row a fetch can actually refresh may offer "fetch to verify" — `canFetch` nil fails CLOSED
/// (no promise), and a Codex row says what would actually move it instead.
func freshnessDetail(_ row: MeterRow, now: Date = Date()) -> String? {
    guard !row.isLive else { return nil }
    if row.isStale, row.reset == "now" {
        return "Window reset · waiting for a current \(sourceLabel(row.source)) sample"
    }
    let fetchClause = row.canFetch == true ? " · fetch to verify"
        : row.provider == "codex" ? " · updates only while a Codex session runs" : ""
    if row.isStale, let seenAt = row.seenAt {
        let age = ageLabel(since: Date(timeIntervalSince1970: seenAt), now: now)
        return "\(sourceLabel(row.source)) last confirmed \(age)\(fetchClause)"
    }
    if row.state == "missing" {
        return "No verified sample for this window\(fetchClause)"
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
    if reset == "now" { return "resets now" }
    if let hours = parseResetHours(reset), let fetchedAt {
        return "resets \(limitTimeLabel(fetchedAt.addingTimeInterval(hours * 3600)))"
    }
    return "resets in \(reset)"
}

/// The reset time always owns the meta slot — "when does this come back" matters most at 100%,
/// exactly when risk text used to displace it — so risk moves up beside the window label. A row
/// with risk but no reset keeps risk in the meta slot rather than going blank.
func metaTexts(risk: String?, reset: String?) -> (labelRisk: String?, meta: String?, metaIsRisk: Bool) {
    guard let reset else { return (nil, risk, risk != nil) }
    return (risk, reset, false)
}

private func groupedByLabel(_ rows: [MeterRow]) -> [(label: String, rows: [MeterRow])] {
    var seen = Set<String>()
    var result: [(label: String, rows: [MeterRow])] = []
    for row in rows where seen.insert(row.label).inserted {
        result.append((row.label, rows.filter { $0.label == row.label }))
    }
    return result
}

private func reportOverflow(_ size: CGSize, availableHeight: CGFloat) {
    guard size.width > expandedSurfaceWidth || size.height > availableHeight else { return }
    let message = "usage-hud: expanded content \(size) exceeds available \(expandedSurfaceWidth)x\(availableHeight)\n"
    FileHandle.standardError.write(Data(message.utf8))
}

/// Header affordance that fires an on-demand API fetch. Fixed footprint so swapping the icon for
/// the in-flight spinner never nudges the header layout; disabled + spinning while a fetch runs.
private struct FetchButton: View {
    @ObservedObject var model: UsageModel

    var body: some View {
        Button(action: { hudLog("[fetch] button tapped"); model.fetchFromAPI() }) {
            ZStack {
                if model.isFetching {
                    ProgressView().controlSize(.mini).scaleEffect(0.7)
                } else {
                    Image(systemName: "icloud.and.arrow.down").font(.system(size: 11))
                }
            }
            .frame(width: 14, height: 14)
            .foregroundColor(.secondary)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(model.isFetching)
        .help("Fetch usage from API")
        .accessibilityLabel("Fetch usage from API")
    }
}

private struct UsageBarView: View {
    let pct: Int
    let fill: Color
    let reduceMotion: Bool

    var body: some View {
        GeometryReader { proxy in
            let clamped = min(max(pct, 0), 100)
            let fillWidth = proxy.size.width * CGFloat(clamped) / 100
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 2).fill(Color.primary.opacity(0.08))
                RoundedRectangle(cornerRadius: 2)
                    .fill(fill)
                    .frame(width: fillWidth)
                    .animation(reduceMotion ? nil : .easeOut(duration: barFillFade), value: fillWidth)
            }
        }
        .frame(height: 4)
    }
}

private let accountLabelWidth: CGFloat = 140
private let meterSpacing: CGFloat = 10
// The expanded shape removes 18 points at each shoulder; this leaves 18 visible points inside it.
private let expandedContentHorizontalPadding: CGFloat = 36
private let meterCellWidth = (
    expandedSurfaceWidth - expandedContentHorizontalPadding * 2 - accountLabelWidth - 12 - meterSpacing * 2
) / 3

private struct MeterCell: View {
    let row: MeterRow
    let pressure: RowPressure?
    let fetchedAt: Date?
    let reduceMotion: Bool

    private var pct: Int { row.pct ?? 0 }

    private var tint: (bar: Color, text: Color)? {
        let severity = PresentationSeverity(pct: pct, pressure: pressure?.pressure)
        guard let bar = severity.barColor, let text = severity.textColor else { return nil }
        return (bar, text)
    }

    private var texts: (labelRisk: String?, meta: String?, metaIsRisk: Bool) {
        guard row.pct != nil else { return (nil, nil, false) }
        return metaTexts(risk: riskText(pressure), reset: resetText(row, fetchedAt: fetchedAt))
    }

    private var meta: Text? {
        let texts = texts
        return texts.meta.map {
            Text($0).foregroundColor(texts.metaIsRisk ? tint?.text ?? .secondary : .secondary)
        }
    }

    private var windowTitle: Text {
        let base = Text(windowLabel(row)).foregroundColor(.secondary)
        guard let risk = texts.labelRisk else { return base }
        return base + Text(" · \(risk)").foregroundColor(tint?.text ?? .secondary)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            // Middle truncation: with a risk suffix present, the ETA tail must survive a squeeze.
            windowTitle
                .font(.system(size: 9, weight: .medium))
                .lineLimit(1)
                .truncationMode(.middle)

            UsageBarView(
                pct: pct,
                fill: row.pct == nil ? .clear : tint?.bar ?? providerColor(row.provider).opacity(0.78),
                reduceMotion: reduceMotion
            )

            HStack(alignment: .firstTextBaseline, spacing: 4) {
                if let pct = row.pct {
                    Text("\(pct)%")
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .monospacedDigit()
                        .foregroundColor(tint?.text ?? .primary)
                        .contentTransition(.numericText())
                } else {
                    Text("No data")
                        .font(.system(size: 9, weight: .semibold, design: .monospaced))
                        .foregroundColor(dangerTextColor)
                }
                Spacer(minLength: 2)
                if let meta {
                    meta.font(.system(size: 8, design: .monospaced))
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            }
        }
        .saturation(row.isStale ? 0 : 1)
        .opacity(row.isStale ? 0.62 : 1)
        .animation(reduceMotion ? nil : .easeOut(duration: microFade), value: row.isStale)
    }
}

private struct MeterSection: View {
    let label: String
    let rows: [MeterRow]
    let pressures: [RowPressure]
    let fetchedAt: Date?
    let reduceMotion: Bool
    let startIndex: Int

    private var staleCaption: String? {
        guard rows.contains(where: \.isStale),
              let oldest = rows.compactMap({ $0.seenAt }).min() else { return nil }
        return "data \(timeFormatter.string(from: Date(timeIntervalSince1970: oldest)))"
    }

    private var statusCaption: String? {
        let parts = [
            primeStatusCopy(rows),
            staleCaption,
            rows.contains { $0.active == true } ? "active" : nil,
        ].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private var sortedRows: [MeterRow] {
        rows.sorted { windowOrder($0.window) < windowOrder($1.window) }
    }

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 6) {
                    Rectangle()
                        .fill(providerColor(rows.first?.provider ?? ""))
                        .frame(width: 2, height: 14)
                    Text(sectionTitle(label: label, provider: rows.first?.provider ?? ""))
                        .font(.system(size: 9, weight: .semibold, design: .monospaced))
                        .tracking(0.7)
                        .foregroundColor(.secondary)
                        .lineLimit(1)
                }
                if let trailing = statusCaption {
                    Text(trailing)
                        .font(.system(size: 8, design: .monospaced))
                        .foregroundColor(.secondary)
                        .padding(.leading, 8)
                }
            }
            .frame(width: accountLabelWidth, alignment: .leading)

            if let unavailable = rows.first(where: { $0.state == "offline" || $0.state == "logged_out" }) {
                Text(availabilityCopy(unavailable))
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundColor(.secondary)
                Spacer(minLength: 0)
            } else {
                HStack(alignment: .center, spacing: meterSpacing) {
                    ForEach(Array(sortedRows.enumerated()), id: \.element.window) { index, row in
                        MeterCell(
                            row: row,
                            pressure: pressures.first { $0.label == row.label && $0.window == row.window },
                            fetchedAt: fetchedAt,
                            reduceMotion: reduceMotion
                        )
                        .frame(width: meterCellWidth)
                        .transition(rowTransition(delay: rowDelay(index)))
                    }
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.vertical, 7)
        .overlay(alignment: .top) { Rectangle().fill(hairline).frame(height: 1) }
    }

    private func rowDelay(_ localIndex: Int) -> Double { Double(startIndex + localIndex) * 0.025 }

    private func rowTransition(delay: Double) -> AnyTransition {
        guard !reduceMotion else { return .opacity }
        return .asymmetric(
            insertion: .opacity.combined(with: .offset(y: 4)).animation(.easeOut(duration: 0.22).delay(delay)),
            removal: .opacity.animation(.easeOut(duration: microFade))
        )
    }
}

struct ExpandedContent: View {
    @ObservedObject var model: UsageModel
    let reduceMotion: Bool

    private var groups: [(label: String, rows: [MeterRow], startIndex: Int)] {
        var result: [(label: String, rows: [MeterRow], startIndex: Int)] = []
        var index = 0
        for (label, rows) in groupedByLabel(model.rows) {
            result.append((label, rows, index))
            index += rows.contains { $0.state == "offline" || $0.state == "logged_out" } ? 1 : rows.count
        }
        return result
    }

    private var status: (word: String, tint: Color?) {
        guard let basis = bubbleBasis(model.bindingPressure) else { return ("WAITING FOR DATA", nil) }
        let severity = basis.severity
        return (severity.statusWord, severity.textColor)
    }

    private var headerMeta: (text: String, warn: Bool) {
        if model.fetchFailed { return ("refresh failed", true) }
        if model.isRefreshing && model.lastFetchAt == nil { return ("updating…", false) }
        guard let last = model.lastFetchAt else { return ("", false) }
        return ("updated \(timeFormatter.string(from: last))", false)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text(status.word)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                    .tracking(1.2)
                    .foregroundColor(status.tint ?? .primary)
                Spacer()
                Text(model.statusText ?? headerMeta.text)
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundColor(headerMeta.warn && model.statusText == nil ? warnTextColor : .secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                FetchButton(model: model)
            }

            if model.firstLaunchFailure {
                Text("usage unavailable · no data yet")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundColor(.secondary)
            } else if model.rows.isEmpty {
                SkeletonView(reduceMotion: reduceMotion)
            } else {
                ForEach(groups, id: \.label) { group in
                    MeterSection(
                        label: group.label,
                        rows: group.rows,
                        pressures: model.rowPressures,
                        fetchedAt: model.lastSnapshotAt,
                        reduceMotion: reduceMotion,
                        startIndex: group.startIndex
                    )
                }
            }
        }
        .frame(
            width: expandedSurfaceWidth - expandedContentHorizontalPadding * 2,
            alignment: .topLeading
        )
        .padding(.horizontal, expandedContentHorizontalPadding)
        .padding(.vertical, 14)
        .background(GeometryReader { proxy in
            Color.clear
                .onAppear { reportMeasuredSize(proxy.size) }
                .onChange(of: proxy.size) { _, size in reportMeasuredSize(size) }
        })
        .frame(maxHeight: hudPanelSize.height, alignment: .top)
    }

    private func reportMeasuredSize(_ size: CGSize) {
        let availableHeight = max(0, hudPanelSize.height - model.notchTopInset)
        reportOverflow(size, availableHeight: availableHeight)
        let height = min(size.height, availableHeight)
        let width = min(size.width, expandedSurfaceWidth)
        if model.expandedContentHeight != height { model.expandedContentHeight = height }
        if model.expandedContentWidth != width { model.expandedContentWidth = width }
    }
}

private struct SkeletonView: View {
    let reduceMotion: Bool
    @State private var dimmed = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(0..<3, id: \.self) { _ in
                VStack(alignment: .leading, spacing: 8) {
                    RoundedRectangle(cornerRadius: 1.5).fill(Color.primary.opacity(0.10)).frame(width: 96, height: 10)
                    Rectangle().fill(hairline).frame(height: 1)
                    RoundedRectangle(cornerRadius: 1.5).fill(Color.primary.opacity(0.07)).frame(height: 3)
                    RoundedRectangle(cornerRadius: 1.5).fill(Color.primary.opacity(0.07)).frame(height: 3)
                }
            }
        }
        .opacity(dimmed ? 0.45 : 1)
        .onAppear { if !reduceMotion { startBreathing() } }
        .onChange(of: reduceMotion) { _, value in
            if value {
                withAnimation(.easeOut(duration: microFade)) { dimmed = false }
            } else {
                startBreathing()
            }
        }
    }

    private func startBreathing() {
        withAnimation(.easeInOut(duration: 0.6).repeatForever(autoreverses: true)) {
            dimmed = true
        }
    }
}
