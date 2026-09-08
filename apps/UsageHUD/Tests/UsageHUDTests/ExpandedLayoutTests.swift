import AppKit
import SwiftUI
import XCTest
@testable import UsageHUD

private func row(_ label: String, _ window: String, active: Bool?) -> MeterRow {
    MeterRow(label: label, provider: label.hasPrefix("cx") ? "codex" : "claude",
             window: window, pct: 42, reset: "3d8h", state: "ok", reason: nil,
             asOf: Date().timeIntervalSince1970, active: active)
}

// The fullest realistic shape: three Claude profiles with 5h/7d/Fable windows plus Codex.
private func fullRows() -> [MeterRow] {
    [
        row("cl·pri", "5h", active: true), row("cl·pri", "7d", active: true), row("cl·pri", "fb", active: true),
        row("cl·endu", "5h", active: false), row("cl·endu", "7d", active: false), row("cl·endu", "fb", active: false),
        row("cl·sid", "5h", active: false), row("cl·sid", "7d", active: false), row("cl·sid", "fb", active: false),
        row("cx", "7d", active: nil),
    ]
}

/// Freshness is decided by the dock script, per row, and arrives as `state` — the app never
/// re-derives it from `asOf`, so there is exactly one threshold in the system.
final class RowStalenessTests: XCTestCase {
    private let now = Date().timeIntervalSince1970

    func testOnlyOkRowsAreLive() {
        let fresh = MeterRow(label: "cl·pri", provider: "claude", window: "7d", pct: 10, reset: "1h",
                             state: "ok", reason: nil, asOf: now, active: true)
        // A stale row keeps its last confirmed value for display but remains non-live.
        let stale = MeterRow(label: "cl·pri", provider: "claude", window: "fb", pct: 67, reset: "3d",
                             state: "stale", reason: nil, asOf: now - 2700, active: true)
        let missing = MeterRow(label: "cl·pri", provider: "claude", window: "5h", pct: nil, reset: nil,
                               state: "missing", reason: nil, asOf: nil, active: true)
        let offline = MeterRow(label: "cx", provider: "codex", window: nil, pct: nil, reset: nil,
                               state: "offline", reason: "no data", asOf: nil, active: nil)
        XCTAssertEqual([fresh, stale, missing, offline].map(\.isLive), [true, false, false, false])
        XCTAssertEqual([fresh, stale, missing, offline].map(\.isStale), [false, true, false, false])
    }

    /// The incident shape: an aged row that still claims `ok` keeps its number. Gating happens once,
    /// in the dock script — a row the script vouched for is not second-guessed here.
    func testAgedOkRowStaysLive() {
        let aged = MeterRow(label: "cl·pri", provider: "claude", window: "7d", pct: 34, reset: "3d",
                            state: "ok", reason: nil, asOf: now - 6 * 3600, active: true)
        XCTAssertTrue(aged.isLive)
        XCTAssertFalse(aged.isStale)
    }

    func testStaleDetailExplainsAgeAndSource() {
        let stale = MeterRow(label: "cl·endu", provider: "claude", window: "fb", pct: nil, reset: "2d",
                             state: "stale", reason: nil, asOf: now - 1200, seenAt: now - 1200,
                             active: false, source: "api", canFetch: true)
        XCTAssertEqual(
            freshnessDetail(stale, now: Date(timeIntervalSince1970: now)),
            "API last confirmed 20m ago · click Refresh to fetch current usage"
        )
    }

    func testStaleCodexDetailNamesTheManualUpdatePath() {
        let stale = MeterRow(label: "cx", provider: "codex", window: "7d", pct: 40, reset: "3d",
                             state: "stale", reason: nil, asOf: now - 7200, seenAt: now - 7200,
                             active: nil, source: "api", canFetch: true)
        XCTAssertEqual(
            freshnessDetail(stale, now: Date(timeIntervalSince1970: now)),
            "API last confirmed 2h ago · click Refresh to fetch current usage"
        )
    }

    /// Unknown fetchability (fixture or a dock script predating the flag) fails closed.
    func testStaleDetailOffersManualRefresh() {
        let stale = MeterRow(label: "cl·pri", provider: "claude", window: "7d", pct: 12, reset: "2d",
                             state: "stale", reason: nil, asOf: now - 1200, seenAt: now - 1200,
                             active: true, source: "api")
        XCTAssertEqual(
            freshnessDetail(stale, now: Date(timeIntervalSince1970: now)),
            "API last confirmed 20m ago · click Refresh to fetch current usage"
        )
        let missing = MeterRow(label: "cl·pri", provider: "claude", window: "5h", pct: nil, reset: nil,
                               state: "missing", reason: nil, asOf: nil, active: true)
        XCTAssertEqual(freshnessDetail(missing), "No local sample for this window · click Refresh to fetch current usage")
    }

    /// A provider the CLI cannot fetch is never told to press Refresh: the button would not move it.
    func testUnfetchableRowNamesTheManualUpdatePath() {
        let stale = MeterRow(label: "cx", provider: "codex", window: "7d", pct: 40, reset: "3d",
                             state: "stale", reason: nil, asOf: now - 1200, seenAt: now - 1200,
                             active: nil, source: "api", canFetch: false)
        XCTAssertEqual(
            freshnessDetail(stale, now: Date(timeIntervalSince1970: now)),
            "API last confirmed 20m ago · use this account in the CLI to update it"
        )
    }

    func testExpiredWindowMarksItsLastZeroAsStale() {
        let expired = MeterRow(label: "cl·endu", provider: "claude", window: "5h", pct: 0, reset: "now",
                               state: "stale", reason: nil, asOf: now, seenAt: now,
                               active: false, source: "api")
        XCTAssertEqual(
            freshnessDetail(expired, now: Date(timeIntervalSince1970: now)),
            "Window reset · waiting for a current API sample"
        )
    }
}

/// The account subtitle answers "can I trust this number?": a failed fetch outranks the age.
final class AccountSubtitleTests: XCTestCase {
    private let now = Date().timeIntervalSince1970

    func testFreshActiveAccountReportsAgeAndLastUsed() {
        let rows = [MeterRow(label: "cl·pri", provider: "claude", window: "5h", pct: 10, reset: "1h",
                             state: "ok", reason: nil, asOf: now, seenAt: now,
                             active: true, source: "api", canFetch: true)]
        let subtitle = accountSubtitle(rows, fetchStatus: "ok", now: Date(timeIntervalSince1970: now))
        XCTAssertEqual(subtitle.text, "just now · last used")
        XCTAssertFalse(subtitle.warning)
    }

    func testFetchFailureLeadsAndKeepsTheLocalSampleAge() {
        let rows = [MeterRow(label: "cx·work", provider: "codex", window: "7d", pct: 40, reset: "3d",
                             state: "stale", reason: nil, asOf: now - 4 * 86_400, seenAt: now - 4 * 86_400,
                             active: nil, source: "rollout", canFetch: false)]
        let subtitle = accountSubtitle(rows, fetchStatus: "fetch-failed", now: Date(timeIntervalSince1970: now))
        XCTAssertEqual(subtitle.text, "Fetch failed · 4d ago")
        XCTAssertTrue(subtitle.warning)
    }

    func testAuthStaleWithoutAnyLocalSampleStandsAlone() {
        let rows = [MeterRow(label: "cl·endu", provider: "claude", window: "5h", pct: nil, reset: nil,
                             state: "missing", reason: nil, asOf: nil,
                             active: false, source: "api", canFetch: true)]
        let subtitle = accountSubtitle(rows, fetchStatus: "auth-stale", now: Date(timeIntervalSince1970: now))
        XCTAssertEqual(subtitle.text, "Token expired")
        XCTAssertTrue(subtitle.warning)
    }
}

/// The header clock reports the weakest confirmation that the unified fetch can refresh.
final class HeaderClockTests: XCTestCase {
    private let now = Date().timeIntervalSince1970

    func testOldestDataDateIncludesFetchableCodexRows() {
        let claude = MeterRow(label: "cl·pri", provider: "claude", window: "7d", pct: 10, reset: "2d",
                              state: "ok", reason: nil, asOf: now - 60, seenAt: now - 60,
                              active: true, source: "api", canFetch: true)
        let codex = MeterRow(label: "cx", provider: "codex", window: "7d", pct: 40, reset: "3d",
                             state: "stale", reason: nil, asOf: now - 18_000, seenAt: now - 18_000,
                             active: nil, source: "api", canFetch: true)
        XCTAssertEqual(oldestDataDate(in: [claude, codex])?.timeIntervalSince1970, now - 18_000)
    }

    func testOldestDataDateFallsBackToAllRowsWhenNothingClaimsFetchability() {
        let newer = MeterRow(label: "cl·pri", provider: "claude", window: "7d", pct: 10, reset: "2d",
                             state: "ok", reason: nil, asOf: now - 60, seenAt: now - 60, active: true)
        let older = MeterRow(label: "cx", provider: "codex", window: "7d", pct: 40, reset: "3d",
                             state: "stale", reason: nil, asOf: now - 18_000, seenAt: now - 18_000,
                             active: nil)
        XCTAssertEqual(oldestDataDate(in: [newer, older])?.timeIntervalSince1970, now - 18_000)
    }
}

final class ExpandedLayoutTests: XCTestCase {
    func testPanelCeilingTracksScreenHeight() {
        XCTAssertEqual(hudPanelHeight(screenHeight: 900), 860)
        XCTAssertEqual(hudPanelHeight(screenHeight: 300), 400)   // floor for tiny screens
    }

    /// A ceiling change while EXPANDED must defer (not drop) the stored-height reset: it applies at
    /// the next collapsed application, even though the ceiling is already current by then.
    @MainActor
    func testCeilingChangeWhileExpandedDefersResetToNextCollapsedPlacement() {
        let savedHeight = hudPanelSize.height
        defer { hudPanelSize.height = savedHeight }

        let model = UsageModel()
        hudPanelSize.height = 500
        model.expandedContentHeight = 480   // measured under the old ceiling
        model.isCollapsed = false

        model.applyPanelCeiling(900)                       // change while expanded
        XCTAssertEqual(hudPanelSize.height, 900)
        XCTAssertEqual(model.expandedContentHeight, 480)   // visible surface untouched

        model.isCollapsed = true
        model.applyPanelCeiling(900)                       // next placement: same ceiling, owed reset
        XCTAssertEqual(model.expandedContentHeight, 900)

        model.expandedContentHeight = 700                  // fresh measurement lands
        model.applyPanelCeiling(900)                       // routine same-screen reposition
        XCTAssertEqual(model.expandedContentHeight, 700)   // no churn
    }

    /// A ceiling INCREASE must let the stored (old-ceiling-clamped) height re-expand on the next
    /// expand cycle: measure under a low ceiling → clamped; raise the ceiling → a fresh
    /// ExpandedContent (what a collapse/expand produces) re-measures and recovers the full height.
    @MainActor
    func testCeilingIncreaseRecoversClampedHeight() {
        let savedHeight = hudPanelSize.height
        defer { hudPanelSize.height = savedHeight }

        let model = UsageModel()
        model.rows = fullRows()

        hudPanelSize.height = 250
        model.expandedContentHeight = hudPanelSize.height
        let lowMeasured = measureExpanded(model)
        XCTAssertEqual(lowMeasured, 250 - model.notchTopInset)

        hudPanelSize.height = 1000
        model.expandedContentHeight = hudPanelSize.height   // what updatePanelCeiling does on change
        let recovered = measureExpanded(model)
        XCTAssertGreaterThan(recovered, lowMeasured)
        XCTAssertLessThan(recovered, 1000)   // settled on true content height, not the ceiling
    }

    /// Account rows use the available width and remain inside the panel ceiling.
    @MainActor
    func testAccountsUseTheAvailableSurface() {
        let model = UsageModel()
        model.rows = fullRows()
        let measured = measureExpanded(model)
        XCTAssertLessThanOrEqual(measured, hudPanelSize.height - model.notchTopInset)
        XCTAssertEqual(model.expandedContentWidth, expandedSurfaceWidth)
    }

    @MainActor
    func testSevenAccountsFitWithoutScreenHeightPanel() {
        let savedHeight = hudPanelSize.height
        defer { hudPanelSize.height = savedHeight }
        hudPanelSize.height = 1000
        let model = UsageModel()
        model.rows = fullRows() + (1...3).map { row("cx·account-\($0)", "7d", active: nil) }
        XCTAssertLessThan(measureExpanded(model), 640)
    }

    @MainActor
    func testLongAccountListKeepsCompactViewport() {
        let savedHeight = hudPanelSize.height
        defer { hudPanelSize.height = savedHeight }
        hudPanelSize.height = 1000
        let model = UsageModel()
        model.rows = fullRows() + (1...20).map { row("cx·account-\($0)", "7d", active: nil) }
        XCTAssertEqual(measureExpanded(model), 640)
    }

    /// Risk labels must fit inside the visible scrolling surface.
    @MainActor
    func testWorstCaseAllRowsAtRiskFitsMinimumCeiling() {
        let model = UsageModel()
        model.rows = fullRows()
        model.rowPressures = model.rows.map {
            RowPressure(label: $0.label, window: $0.window ?? "", pct: $0.pct ?? 0, active: $0.active != false,
                        burn: 4, hoursToReset: 120, projected: 95, eta100: 100, pressure: .red)
        }
        let measured = measureExpanded(model)
        XCTAssertLessThanOrEqual(measured, hudPanelSize.height - model.notchTopInset)
    }

    @MainActor
    private func measureExpanded(_ model: UsageModel) -> CGFloat {
        let hosting = NSHostingView(rootView: ExpandedContent(model: model, reduceMotion: true))
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: hudPanelSize),
                              styleMask: .borderless, backing: .buffered, defer: false)
        window.contentView = hosting
        window.orderFront(nil)
        defer { window.orderOut(nil) }
        let deadline = Date().addingTimeInterval(2)
        while model.expandedContentHeight == hudPanelSize.height && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
        return model.expandedContentHeight
    }

    /// Three Claude profiles with Fable plus Codex must
    /// measure under `hudPanelSize.height`
    /// (screen-relative on this machine). The measurement lands via ExpandedContent's own
    /// GeometryReader, so this exercises the exact pipeline that sizes the live panel; an overflow
    /// also prints the true size to stderr.
    @MainActor
    func testFullRowShapeFitsPanelCeiling() {
        let model = UsageModel()
        model.rows = fullRows()
        // Clamped-at-ceiling means either overflow or no measurement — both are failures.
        XCTAssertLessThan(measureExpanded(model), hudPanelSize.height)
        XCTAssertLessThan(model.expandedContentWidth, hudPanelSize.width)
    }
}
