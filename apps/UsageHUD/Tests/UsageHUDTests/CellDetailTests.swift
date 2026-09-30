import XCTest
@testable import UsageHUD

/// The lines under a meter bar. Times are local; `now` is 10:00 today so same-day times carry no
/// weekday and the fixture never crosses midnight by accident.
final class CellDetailTests: XCTestCase {
    private let now = Calendar.current.date(bySettingHour: 10, minute: 0, second: 0, of: Date())!

    private func row(reset: String = "2h", state: String = "ok") -> MeterRow {
        MeterRow(label: "cl·pri", provider: "claude", window: "5h", pct: 60, reset: reset,
                 state: state, reason: nil, asOf: now.timeIntervalSince1970, active: true)
    }

    private func pressure(_ cls: PressureClass, eta100: Double? = nil, projected: Double? = nil,
                          hoursToReset: Double = 2) -> RowPressure {
        RowPressure(label: "cl·pri", window: "5h", pct: 60, active: true, burn: 30,
                    hoursToReset: hoursToReset, projected: projected, eta100: eta100, pressure: cls)
    }

    private func texts(_ lines: [DetailLine]) -> [String] { lines.map(\.text) }

    func testGreenShowsOnlyTheReset() {
        let lines = cellDetail(row: row(), pressure: pressure(.green), fetchedAt: now, now: now)
        XCTAssertEqual(lines, [DetailLine(text: "Resets 12:00", risk: false)])
    }

    func testRedShowsRunOutThenResetWithTheGap() {
        let lines = cellDetail(row: row(), pressure: pressure(.red, eta100: 1.3), fetchedAt: now, now: now)
        XCTAssertEqual(lines, [DetailLine(text: "Runs out 11:18", risk: true),
                               DetailLine(text: "Resets 12:00 · 42m later", risk: false)])
    }

    func testGapMatchesTheTwoMinutesOnScreen() {
        // eta 1.295 h is 11:17:42, shown as 11:17; the reset is 12:00, so the gap reads 43m.
        let lines = cellDetail(row: row(), pressure: pressure(.red, eta100: 1.295), fetchedAt: now, now: now)
        XCTAssertEqual(texts(lines), ["Runs out 11:17", "Resets 12:00 · 43m later"])
    }

    func testSameMinuteDropsTheGap() {
        // 1.999 h runs out at 11:59:56, one shown minute before the 12:00 reset. A fetch 1 s
        // later with 1.9999 h runs out at 12:00:00, the same minute as the reset.
        let lines = cellDetail(row: row(reset: "2h"), pressure: pressure(.red, eta100: 1.999), fetchedAt: now, now: now)
        XCTAssertEqual(texts(lines), ["Runs out 11:59", "Resets 12:00 · 1m later"])
        let same = cellDetail(row: row(reset: "2h"), pressure: pressure(.red, eta100: 1.9999), fetchedAt: now.addingTimeInterval(1), now: now)
        XCTAssertEqual(texts(same), ["Runs out 12:00", "Resets 12:00"])
    }

    func testAmberKeepsTheResetAndAddsThePlainProjection() {
        let lines = cellDetail(row: row(), pressure: pressure(.amber, projected: 91.6), fetchedAt: now, now: now)
        XCTAssertEqual(lines, [DetailLine(text: "Resets 12:00", risk: false),
                               DetailLine(text: "On pace for 92% by then", risk: true)])
    }

    func testAmberProjectionIsClampedTo100() {
        let lines = cellDetail(row: row(), pressure: pressure(.amber, projected: 130), fetchedAt: now, now: now)
        XCTAssertEqual(lines.last?.text, "On pace for 100% by then")
    }

    func testRedOnAnotherDayCarriesTheWeekday() {
        let weekday = DateFormatter()
        weekday.dateFormat = "EEE HH:mm"
        let runsOut = weekday.string(from: now.addingTimeInterval(20 * 3600))
        let resets = weekday.string(from: now.addingTimeInterval(30 * 3600))
        let lines = cellDetail(row: row(reset: "30h"), pressure: pressure(.red, eta100: 20, hoursToReset: 30),
                               fetchedAt: now, now: now)
        XCTAssertEqual(texts(lines), ["Runs out \(runsOut)", "Resets \(resets) · 10h later"])
    }

    func testWithoutAFetchTimeOnlyTheRelativeResetShows() {
        let lines = cellDetail(row: row(), pressure: pressure(.red, eta100: 1.3), fetchedAt: nil, now: now)
        XCTAssertEqual(texts(lines), ["Resets in 2h"])
    }

    func testAnOldSampleShowsNoRisk() {
        let lines = cellDetail(row: row(state: "stale"), pressure: pressure(.red, eta100: 1.3), fetchedAt: now, now: now)
        XCTAssertEqual(texts(lines), ["Resets 12:00"])
    }

    func testGapLabel() {
        XCTAssertEqual(gapLabel(hours: 0.75), "45m")
        XCTAssertEqual(gapLabel(hours: 1 + 10.0 / 60), "1h 10m")
        XCTAssertEqual(gapLabel(hours: 2), "2h")
        XCTAssertEqual(gapLabel(hours: 51), "2d 3h")
        XCTAssertEqual(gapLabel(hours: 48), "2d")
        XCTAssertEqual(gapLabel(hours: -1), "0m")
    }
}
