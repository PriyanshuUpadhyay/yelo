import XCTest
@testable import UsageHUD

final class HistoryStoreTests: XCTestCase {
    private func sample(_ pct: Int, daysAgo: Double, now: Double = 1_000_000_000) -> HistorySample {
        HistorySample(label: "cl", window: "5h", pct: pct, asOf: now - daysAgo * 86400)
    }

    func testPruneDropsOlderThanSevenDays() {
        let now = Date(timeIntervalSince1970: 1_000_000_000)
        let kept = pruneHistory([sample(10, daysAgo: 8), sample(20, daysAgo: 1)], now: now)
        XCTAssertEqual(kept.map { $0.pct }, [20])
    }

    func testPruneCapsCount() {
        let now = Date(timeIntervalSince1970: 1_000_000_000)
        let many = (0..<10).map { sample($0, daysAgo: Double(10 - $0) / 24) } // all within 7d, oldest first
        let kept = pruneHistory(many, now: now, maxCount: 3)
        XCTAssertEqual(kept.count, 3)
        XCTAssertEqual(kept.map { $0.pct }, [7, 8, 9]) // newest 3 retained, chronological
    }

    func testSamplesFromRowsSkipsOfflineAndNilPct() {
        let now = Date(timeIntervalSince1970: 500)
        let rows = [
            MeterRow(label: "cl", provider: "claude", window: "5h", pct: 42, reset: "1h", state: "ok", reason: nil, asOf: 480, active: true),
            MeterRow(label: "cx", provider: "codex", window: nil, pct: nil, reset: nil, state: "offline", reason: "not set up", asOf: nil, active: nil),
        ]
        let out = samplesFromRows(rows, now: now)
        XCTAssertEqual(out, [HistorySample(label: "cl", window: "5h", pct: 42, asOf: 480)]) // uses row.asOf
    }

    func testSamplesFromRowsFallsBackToNow() {
        let now = Date(timeIntervalSince1970: 500)
        let rows = [MeterRow(label: "cl", provider: "claude", window: "7d", pct: 10, reset: "5d", state: "ok", reason: nil, asOf: nil, active: nil)]
        XCTAssertEqual(samplesFromRows(rows, now: now).first?.asOf, 500)
    }

    func testOldestDataDateUsesWeakestConfirmation() {
        let rows = [
            MeterRow(label: "cl", provider: "claude", window: "5h", pct: 10, reset: "1h",
                     state: "ok", reason: nil, asOf: 400, seenAt: 900, active: true),
            MeterRow(label: "cx", provider: "codex", window: "7d", pct: 20, reset: "5d",
                     state: "ok", reason: nil, asOf: 500, seenAt: 800, active: nil),
        ]
        // Weakest CONFIRMATION, not weakest value-change: a row whose value last moved at 400 but
        // was re-confirmed at 900 is current, and must not be reported as data from 400.
        XCTAssertEqual(oldestDataDate(in: rows), Date(timeIntervalSince1970: 800))
        XCTAssertNil(oldestDataDate(in: []))
    }

    func testSamplesFromRowsSkipsGatedRows() {
        let now = Date(timeIntervalSince1970: 500)
        let rows = [
            MeterRow(label: "cl", provider: "claude", window: "5h", pct: nil, reset: "1h",
                     state: "stale", reason: nil, asOf: 100, active: true),
            MeterRow(label: "cl", provider: "claude", window: "7d", pct: nil, reset: nil,
                     state: "missing", reason: nil, asOf: nil, active: true),
        ]
        XCTAssertEqual(samplesFromRows(rows, now: now), [])
    }

    func testAppendRoundTripsThroughDisk() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let store = HistoryStore(directory: dir)
        let now = Date(timeIntervalSince1970: 1_000_000_000)
        _ = store.append([HistorySample(label: "cl", window: "5h", pct: 10, asOf: now.timeIntervalSince1970)], now: now)
        let reloaded = HistoryStore(directory: dir).load()
        XCTAssertEqual(reloaded.map { $0.pct }, [10])
        try? FileManager.default.removeItem(at: dir)
    }

    func testLoadMissingFileReturnsEmpty() {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        XCTAssertEqual(HistoryStore(directory: dir).load(), [])
    }

    func testAppendSkipsDuplicateSamples() {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let store = HistoryStore(directory: dir)
        let now = Date(timeIntervalSince1970: 1_000_000_000)
        let snapshot = [HistorySample(label: "cl", window: "5h", pct: 10, asOf: now.timeIntervalSince1970)]
        _ = store.append(snapshot, now: now)
        let second = store.append(snapshot, now: now)
        XCTAssertEqual(second.count, 1)
        try? FileManager.default.removeItem(at: dir)
    }

    func testCapByteBudgetDropsOldestUntilFits() {
        let many = (0..<50).map { i in HistorySample(label: "cl", window: "5h", pct: i, asOf: Double(i)) }
        let capped = capToByteBudget(many, maxBytes: 300)
        let data = try! JSONEncoder().encode(capped)
        XCTAssertLessThanOrEqual(data.count, 300)
        XCTAssertLessThan(capped.count, many.count)
        XCTAssertEqual(capped.last?.pct, 49) // newest sample always retained
    }

    func testLoadCorruptJSONReturnsEmptyThenSaveWorks() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try "not json".data(using: .utf8)!.write(to: dir.appendingPathComponent("history.json"))
        let store = HistoryStore(directory: dir)
        XCTAssertEqual(store.load(), [])
        let now = Date(timeIntervalSince1970: 1_000_000_000)
        let saved = store.append([HistorySample(label: "cl", window: "5h", pct: 5, asOf: now.timeIntervalSince1970)], now: now)
        XCTAssertEqual(saved.map { $0.pct }, [5])
        try? FileManager.default.removeItem(at: dir)
    }

    func testPruneKeepsSampleExactlyAtSevenDayCutoff() {
        let now = Date(timeIntervalSince1970: 1_000_000_000)
        let atCutoff = HistorySample(label: "cl", window: "5h", pct: 5, asOf: now.timeIntervalSince1970 - 7 * 86400)
        let kept = pruneHistory([atCutoff], now: now)
        XCTAssertEqual(kept.map { $0.pct }, [5]) // inclusive cutoff: exactly 7d old is kept, not dropped
    }

    func testDedupDoesNotCollideAcrossPipeContainingFields() {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let store = HistoryStore(directory: dir)
        let now = Date(timeIntervalSince1970: 1_000_000_000)
        let asOf = now.timeIntervalSince1970
        // Concatenated "label|window|asOf" strings collide even though the tuples are distinct.
        let a = HistorySample(label: "a|b", window: "c", pct: 1, asOf: asOf)
        let b = HistorySample(label: "a", window: "b|c", pct: 2, asOf: asOf)
        let saved = store.append([a, b], now: now)
        XCTAssertEqual(saved.count, 2)
        try? FileManager.default.removeItem(at: dir)
    }

    func testDedupKeepsSamplesWithDifferentAsOf() {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let store = HistoryStore(directory: dir)
        let now = Date(timeIntervalSince1970: 1_000_000_000)
        let asOf = now.timeIntervalSince1970
        let a = HistorySample(label: "cl", window: "5h", pct: 10, asOf: asOf - 60)
        let b = HistorySample(label: "cl", window: "5h", pct: 12, asOf: asOf)
        let saved = store.append([a, b], now: now)
        XCTAssertEqual(saved.map { $0.pct }, [10, 12])
        try? FileManager.default.removeItem(at: dir)
    }
}
