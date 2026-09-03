import XCTest
@testable import UsageHUD

private func sample(_ pct: Int, minutesAgo: Double, now: Double = 1_000_000) -> HistorySample {
    HistorySample(label: "cl", window: "5h", pct: pct, asOf: now - minutesAgo * 60)
}

final class PressureMathTests: XCTestCase {
    func testSegmentDropStartsNewSegment() {
        let s = [sample(80, minutesAgo: 90), sample(85, minutesAgo: 60), sample(10, minutesAgo: 30), sample(20, minutesAgo: 0)]
        XCTAssertEqual(currentSegment(s).map { $0.pct }, [10, 20])
    }

    func testSegmentNoDropKeepsAll() {
        let s = [sample(10, minutesAgo: 60), sample(20, minutesAgo: 30), sample(30, minutesAgo: 0)]
        XCTAssertEqual(currentSegment(s).count, 3)
    }

    func testBurnRatePctPerHour() {
        // 30% over 60min = 30%/hr
        let s = [sample(10, minutesAgo: 60), sample(40, minutesAgo: 0)]
        XCTAssertEqual(burnRate(s, lookbackHours: 1)!, 30, accuracy: 1e-6)
    }

    func testBurnRateNeedsTwoSamples() {
        XCTAssertNil(burnRate([sample(40, minutesAgo: 0)], lookbackHours: 1))
    }

    func testBurnRateNeedsTenMinuteSpan() {
        let s = [sample(10, minutesAgo: 5), sample(40, minutesAgo: 0)]
        XCTAssertNil(burnRate(s, lookbackHours: 1))
    }

    func testBurnRateOnePointDeltaIsQuantizationNoise() {
        let s = [sample(3, minutesAgo: 43), sample(4, minutesAgo: 0)]
        XCTAssertNil(burnRate(s, lookbackHours: 1))
        // Two full points is signal again.
        let s2 = [sample(3, minutesAgo: 43), sample(5, minutesAgo: 0)]
        XCTAssertNotNil(burnRate(s2, lookbackHours: 1))
    }

    func testWeeklyAveragePace() {
        // 5% used with 94h to reset → 74h elapsed → 0.068%/h
        XCTAssertEqual(weeklyAveragePace(pct: 5, hoursToReset: 94)!, 5.0 / 74.0, accuracy: 1e-9)
        // <24h elapsed: early-window division explodes → no trend
        XCTAssertNil(weeklyAveragePace(pct: 3, hoursToReset: 164))
    }

    func testClassifyRedWhenEtaBeforeReset() {
        // pct 50, burn 20/hr → eta100 = 2.5h; reset in 5h → red
        let (_, eta, pressure) = classify(pct: 50, burn: 20, hoursToReset: 5)
        XCTAssertEqual(eta!, 2.5, accuracy: 1e-6)
        XCTAssertEqual(pressure, .red)
    }

    func testClassifyAmberWhenProjectedHigh() {
        // pct 80, burn 5/hr, reset in 2h → eta100 = 4h (> 2h, not red); projected = 90 (≥85) → amber
        let (proj, _, pressure) = classify(pct: 80, burn: 5, hoursToReset: 2)
        XCTAssertEqual(proj!, 90, accuracy: 1e-6)
        XCTAssertEqual(pressure, .amber)
    }

    func testClassifyGreenLowProjection() {
        let (_, _, pressure) = classify(pct: 30, burn: 2, hoursToReset: 3)
        XCTAssertEqual(pressure, .green)
    }

    func testClassifyGreenWhenNoBurn() {
        let (proj, eta, pressure) = classify(pct: 95, burn: nil, hoursToReset: 1)
        XCTAssertNil(proj); XCTAssertNil(eta); XCTAssertEqual(pressure, .green)
    }

    func testClassifyAmberWhenBurnNonPositiveButProjectedHigh() {
        // burn -3 still yields a projected (95 - 3 = 92, ≥85) even though eta100 is nil (burn not > 0)
        let (proj, eta, pressure) = classify(pct: 95, burn: -3, hoursToReset: 1)
        XCTAssertEqual(proj!, 92, accuracy: 1e-6)
        XCTAssertNil(eta)
        XCTAssertEqual(pressure, .amber)
    }

    func testClassifyAmberWhenZeroBurnHighPct() {
        let (proj, eta, pressure) = classify(pct: 95, burn: 0, hoursToReset: 1)
        XCTAssertEqual(proj!, 95, accuracy: 1e-6)
        XCTAssertNil(eta)
        XCTAssertEqual(pressure, .amber)
    }

    func testClassifyGreenWhenResetNilWithPositiveBurn() {
        // no reset info → no projected (needs both burn and reset), but eta100 still computable from burn alone
        let (proj, eta, pressure) = classify(pct: 50, burn: 10, hoursToReset: nil)
        XCTAssertNil(proj)
        XCTAssertEqual(eta!, 5, accuracy: 1e-6)
        XCTAssertEqual(pressure, .green)
    }

    func testClassifyRedAtFullPct() {
        let (proj, eta, pressure) = classify(pct: 100, burn: 10, hoursToReset: 1)
        XCTAssertEqual(proj!, 110, accuracy: 1e-6)
        XCTAssertEqual(eta!, 0, accuracy: 1e-6)
        XCTAssertEqual(pressure, .red)
    }

    func testBindingRowWorstPressureWins() {
        let green = RowPressure(label: "a", window: "5h", pct: 40, active: true, burn: 1, hoursToReset: 3, projected: 43, eta100: 60, pressure: .green)
        let red = RowPressure(label: "b", window: "5h", pct: 60, active: true, burn: 20, hoursToReset: 5, projected: 160, eta100: 2, pressure: .red)
        XCTAssertEqual(bindingRow([green, red])?.label, "b")
    }

    func testBindingRowExcludesInactive() {
        let inactiveRed = RowPressure(label: "a", window: "5h", pct: 90, active: false, burn: 20, hoursToReset: 5, projected: 190, eta100: 1, pressure: .red)
        let activeGreen = RowPressure(label: "b", window: "5h", pct: 40, active: true, burn: 1, hoursToReset: 3, projected: 43, eta100: 60, pressure: .green)
        XCTAssertEqual(bindingRow([inactiveRed, activeGreen])?.label, "b")
    }

    func testBindingRowTieBreakByProjectedThenPct() {
        let a = RowPressure(label: "a", window: "5h", pct: 50, active: true, burn: 10, hoursToReset: 2, projected: 90, eta100: 5, pressure: .amber)
        let b = RowPressure(label: "b", window: "5h", pct: 55, active: true, burn: 10, hoursToReset: 2, projected: 90, eta100: 4.5, pressure: .amber)
        XCTAssertEqual(bindingRow([a, b])?.label, "b") // equal projected → higher pct
    }

    func testBindingRowNilWhenEmpty() {
        XCTAssertNil(bindingRow([]))
    }

    func testRowPressuresUsesWindowLookback() {
        let rows = [MeterRow(label: "cl", provider: "claude", window: "5h", pct: 60, reset: "2h", state: "ok", reason: nil, asOf: 1_000_000, active: true)]
        let history = [
            HistorySample(label: "cl", window: "5h", pct: 30, asOf: 1_000_000 - 3600),
            HistorySample(label: "cl", window: "5h", pct: 60, asOf: 1_000_000),
        ]
        let rp = rowPressures(rows: rows, history: history)
        XCTAssertEqual(rp.count, 1)
        XCTAssertEqual(rp[0].burn!, 30, accuracy: 1e-6)   // 30% over 60min
        XCTAssertEqual(rp[0].pressure, .red)              // eta100 = 40/30 ≈ 1.33h < 2h reset
    }

    /// A gated row must not contribute a trend: history it can't vouch for is exactly how a
    /// stale meter would keep projecting as if it were live.
    func testRowPressuresSkipsGatedRows() {
        let rows = [MeterRow(label: "cl", provider: "claude", window: "5h", pct: nil, reset: "2h",
                             state: "stale", reason: nil, asOf: 1_000_000, active: true)]
        let history = [
            HistorySample(label: "cl", window: "5h", pct: 30, asOf: 1_000_000 - 3600),
            HistorySample(label: "cl", window: "5h", pct: 60, asOf: 1_000_000),
        ]
        XCTAssertTrue(rowPressures(rows: rows, history: history).isEmpty)
    }

    func testRowPressuresFableWindowTakesWeeklyPace() {
        let rows = [MeterRow(label: "cl", provider: "claude", window: "fb", pct: 60, reset: "3d", state: "ok", reason: nil, asOf: 1_000_000, active: true)]
        let rp = rowPressures(rows: rows, history: [])
        XCTAssertEqual(rp.count, 1)
        XCTAssertEqual(rp[0].burn!, 60.0 / 96.0, accuracy: 1e-9)   // 60% over 96h elapsed
        XCTAssertEqual(rp[0].pressure, .red)   // eta100 = 64h < 72h reset
    }

    /// Regression, from the live traces that red-walled weekly meters: first a lone 3→4 tick,
    /// then a real 3→5 creep inside the lookback hour — both extrapolated one active hour across
    /// a multi-day horizon. Under whole-window pace, low pct deep into the window is GREEN, and
    /// short-lookback history is ignored entirely for 7d windows.
    func testWeeklyLowPctDeepInWindowIsGreen() {
        let rows = [MeterRow(label: "cl·pri", provider: "claude", window: "7d", pct: 5, reset: "3d22h",
                             state: "ok", reason: nil, asOf: 1_000_000, active: true)]
        let history = [   // the old 6h-lookback doom signal: +2 points in the last hour
            HistorySample(label: "cl·pri", window: "7d", pct: 3, asOf: 1_000_000 - 3600),
            HistorySample(label: "cl·pri", window: "7d", pct: 5, asOf: 1_000_000),
        ]
        let rp = rowPressures(rows: rows, history: history)
        XCTAssertEqual(rp.count, 1)
        // pace = 5/74 ≈ 0.068%/h → eta100 ≈ 1406h ≫ 94h reset
        XCTAssertEqual(rp[0].burn!, 5.0 / 74.0, accuracy: 1e-9)
        XCTAssertEqual(rp[0].pressure, .green)
    }

    /// True positive under pace: 60% used with 74h elapsed → eta100 ≈ 49h < 94h reset → red.
    func testWeeklyHighPaceStillFiresRed() {
        let rows = [MeterRow(label: "cl·pri", provider: "claude", window: "7d", pct: 60, reset: "3d22h",
                             state: "ok", reason: nil, asOf: 1_000_000, active: true)]
        let rp = rowPressures(rows: rows, history: [])
        XCTAssertEqual(rp.count, 1)
        XCTAssertEqual(rp[0].eta100!, 40.0 / (60.0 / 74.0), accuracy: 1e-6)   // ≈49.3h
        XCTAssertEqual(rp[0].pressure, .red)
    }

    /// <24h into the weekly window there is no trend — severity bands take over (bubbleBasis).
    func testWeeklyEarlyWindowHasNoTrend() {
        let rows = [MeterRow(label: "cl·pri", provider: "claude", window: "7d", pct: 3, reset: "6d20h",
                             state: "ok", reason: nil, asOf: 1_000_000, active: true)]
        let rp = rowPressures(rows: rows, history: [])
        XCTAssertEqual(rp.count, 1)
        XCTAssertNil(rp[0].burn)
        XCTAssertEqual(rp[0].pressure, .green)
    }

    func testRowPressuresUnknownWindowYieldsNoBurn() {
        let rows = [MeterRow(label: "cl", provider: "claude", window: "1h", pct: 60, reset: "2h", state: "ok", reason: nil, asOf: 1_000_000, active: true)]
        let history = [
            HistorySample(label: "cl", window: "1h", pct: 30, asOf: 1_000_000 - 3600),
            HistorySample(label: "cl", window: "1h", pct: 60, asOf: 1_000_000),
        ]
        let rp = rowPressures(rows: rows, history: history)
        XCTAssertEqual(rp.count, 1)
        XCTAssertNil(rp[0].burn)
        XCTAssertEqual(rp[0].pressure, .green)
    }

    func testAnnotationFormat() {
        let fmt = DateFormatter()
        fmt.dateFormat = "HH:mm"
        fmt.timeZone = TimeZone(identifier: "UTC")
        let now = Date(timeIntervalSince1970: 0) // 00:00 UTC
        // burn 12/hr, eta100 = 3h → 03:00
        XCTAssertEqual(pressureAnnotation(burn: 12.4, eta100: 3, now: now, formatter: fmt), "+12%/hr →100% ~03:00")
    }

    func testLimitTimeLabelSameDayHasNoWeekday() {
        let now = Date()
        let eta = now.addingTimeInterval(60)
        XCTAssertNil(limitTimeLabel(eta, now: now).range(of: "[A-Za-z]", options: .regularExpression))
    }

    func testLimitTimeLabelOtherDayHasWeekday() {
        let now = Date()
        let eta = now.addingTimeInterval(48 * 3600)
        XCTAssertNotNil(limitTimeLabel(eta, now: now).range(of: "[A-Za-z]", options: .regularExpression))
    }

    private func mkRP(pct: Int, burn: Double?, pressure: PressureClass) -> RowPressure {
        RowPressure(label: "a", window: "5h", pct: pct, active: true, burn: burn,
                    hoursToReset: 2, projected: nil, eta100: nil, pressure: pressure)
    }

    func testBubbleBasisNoTrendUsesRawSeverity() {
        // 95% first launch: no history → burn nil → classify returned .green, but the bubble must
        // tint from the raw 95% band, not green.
        XCTAssertEqual(bubbleBasis(mkRP(pct: 95, burn: nil, pressure: .green)), .severity(95))
    }

    func testBubbleBasisWithTrendUsesPressure() {
        XCTAssertEqual(bubbleBasis(mkRP(pct: 60, burn: 20, pressure: .red)), .pressure(.red))
    }

    func testBubbleBasisNilBinding() {
        XCTAssertNil(bubbleBasis(nil))
    }
}
