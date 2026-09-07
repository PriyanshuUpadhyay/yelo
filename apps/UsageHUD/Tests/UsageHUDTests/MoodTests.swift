import XCTest
@testable import UsageHUD

private func liveRow(_ pct: Int) -> MeterRow {
    MeterRow(label: "cl·pri", provider: "claude", window: "5h", pct: pct, reset: "2h",
             state: "ok", reason: nil, asOf: Date().timeIntervalSince1970, active: true)
}

private func staleRow(_ pct: Int) -> MeterRow {
    MeterRow(label: "cl·pri", provider: "claude", window: "5h", pct: pct, reset: "2h",
             state: "stale", reason: nil, asOf: Date().timeIntervalSince1970, active: true)
}

/// `burn: nil` is the no-trend shape, so the face falls back to the raw pct severity band.
private func pressure(pct: Int, burn: Double?, klass: PressureClass) -> RowPressure {
    RowPressure(label: "cl·pri", window: "5h", pct: pct, active: true, burn: burn,
                hoursToReset: 2, projected: burn.map { Double(pct) + $0 * 2 }, eta100: nil,
                pressure: klass)
}

final class UsageMoodTests: XCTestCase {
    func testNoSnapshotYetSleeps() {
        XCTAssertEqual(
            usageMood(hasLoadedSnapshot: false, isFetching: false, rows: [], pressures: []),
            .asleep
        )
    }

    /// A loaded snapshot whose rows are all stale has nothing current to have an opinion about.
    func testLoadedButOnlyStaleRowsSleeps() {
        XCTAssertEqual(
            usageMood(hasLoadedSnapshot: true, isFetching: false, rows: [staleRow(80)], pressures: []),
            .asleep
        )
    }

    /// Fetching outranks every data verdict, including one that would otherwise be danger.
    func testFetchingThinksEvenWithDangerData() {
        XCTAssertEqual(
            usageMood(hasLoadedSnapshot: true, isFetching: true, rows: [liveRow(95)],
                       pressures: [pressure(pct: 95, burn: nil, klass: .green)]),
            .thinking
        )
    }

    func testLiveRowsWithoutPressuresAreCalm() {
        XCTAssertEqual(
            usageMood(hasLoadedSnapshot: true, isFetching: false, rows: [liveRow(20)], pressures: []),
            .calm
        )
    }

    func testAmberPressureWarns() {
        XCTAssertEqual(
            usageMood(hasLoadedSnapshot: true, isFetching: false, rows: [liveRow(40)],
                       pressures: [pressure(pct: 40, burn: 20, klass: .amber)]),
            .warn
        )
    }

    func testNoTrendSeventyFivePercentWarns() {
        XCTAssertEqual(
            usageMood(hasLoadedSnapshot: true, isFetching: false, rows: [liveRow(75)],
                       pressures: [pressure(pct: 75, burn: nil, klass: .green)]),
            .warn
        )
    }

    func testRedPressureIsDanger() {
        XCTAssertEqual(
            usageMood(hasLoadedSnapshot: true, isFetching: false, rows: [liveRow(40)],
                       pressures: [pressure(pct: 40, burn: 30, klass: .red)]),
            .danger
        )
    }

    func testNoTrendNinetyTwoPercentIsDanger() {
        XCTAssertEqual(
            usageMood(hasLoadedSnapshot: true, isFetching: false, rows: [liveRow(92)],
                       pressures: [pressure(pct: 92, burn: nil, klass: .green)]),
            .danger
        )
    }

    func testCaptionsNameEachMood() {
        XCTAssertEqual(Mood.calm.caption, "All calm")
        XCTAssertEqual(Mood.warn.caption, "Running warm")
        XCTAssertEqual(Mood.danger.caption, "Ease off")
        XCTAssertEqual(Mood.asleep.caption, "No fresh usage")
        XCTAssertEqual(Mood.thinking.caption, "Fetching from API…")
    }
}
