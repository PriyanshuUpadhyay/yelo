import Foundation
import XCTest
@testable import UsageHUD

final class UsageDataIntegrationTests: XCTestCase {
    func testFableWeeklyComesFromModelScopedAPICache() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let claude = home.appendingPathComponent(".claude/.profiles/pri")
        try FileManager.default.createDirectory(at: claude, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: home) }

        let base = """
        {"five_hour":{"used_percentage":10,"resets_at":4102444800},"seven_day":{"used_percentage":20,"resets_at":4102444800},"ts":2000,"activity_at":2000,"source":"statusline"}
        """
        let statuslineFable = """
        {"five_hour":{"used_percentage":10,"resets_at":4102444800},"seven_day":{"used_percentage":0,"resets_at":4102445800},"ts":3000,"activity_at":3000,"source":"statusline"}
        """
        let apiFable = """
        {"five_hour":{"used_percentage":10,"resets_at":4102444800},"seven_day":{"used_percentage":100,"resets_at":4102444800},"ts":2500,"fetched_at":2500,"source":"api"}
        """
        try Data(base.utf8).write(to: claude.appendingPathComponent(".usage-cache.json"))
        try Data(statuslineFable.utf8).write(to: claude.appendingPathComponent(".usage-cache-fable.json"))
        try Data(apiFable.utf8).write(to: claude.appendingPathComponent(".usage-api-cache-fable.json"))

        let result = try runUsageData(home: home)
        let rows = try JSONDecoder().decode([MeterRow].self, from: result)
        let fable = try XCTUnwrap(rows.first { $0.label == "cl·pri" && $0.window == "fb" })

        XCTAssertEqual(fable.pct, 100)
        XCTAssertEqual(fable.source, "api")
    }

    func testExpiredWindowDoesNotBecomeLiveZero() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let claude = home.appendingPathComponent(".claude/.profiles/pri")
        try FileManager.default.createDirectory(at: claude, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: home) }

        let now = Int(Date().timeIntervalSince1970)
        let expired = """
        {"five_hour":{"used_percentage":0,"resets_at":1},"seven_day":{"used_percentage":25,"resets_at":4102444800},"ts":\(now),"fetched_at":\(now),"source":"api"}
        """
        try Data(expired.utf8).write(to: claude.appendingPathComponent(".usage-api-cache.json"))

        let result = try runUsageData(home: home)
        let rows = try JSONDecoder().decode([MeterRow].self, from: result)
        let session = try XCTUnwrap(rows.first { $0.label == "cl·pri" && $0.window == "5h" })

        XCTAssertEqual(session.state, "stale")
        XCTAssertEqual(session.pct, 0)
        XCTAssertEqual(session.reset, "now")
        XCTAssertEqual(session.source, "api")
    }

    /// Fetchability is a per-row fact from the data feed. The unified fetch script reports both
    /// Claude and Codex, including accounts that have no cached data yet.
    func testRowsCarryFetchabilityPerProvider() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let claude = home.appendingPathComponent(".claude/.profiles/pri")
        // No caches: this profile emits the offline row shape.
        let emptyClaude = home.appendingPathComponent(".claude/.profiles/endu")
        let codex = home.appendingPathComponent(".codex/sessions/2026/08/03")
        try FileManager.default.createDirectory(at: claude, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: emptyClaude, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: codex, withIntermediateDirectories: true)
        try signIn(codexHome: home.appendingPathComponent(".codex"))
        defer { try? FileManager.default.removeItem(at: home) }

        let now = Int(Date().timeIntervalSince1970)
        let base = """
        {"five_hour":{"used_percentage":10,"resets_at":4102444800},"seven_day":{"used_percentage":20,"resets_at":4102444800},"ts":\(now),"fetched_at":\(now),"source":"api"}
        """
        let rollout = """
        {"payload":{"rate_limits":{"primary":{"used_percent":42,"window_minutes":10080,"resets_at":4102444800},"secondary":null}}}
        """
        try Data(base.utf8).write(to: claude.appendingPathComponent(".usage-api-cache.json"))
        try Data(rollout.utf8).write(to: codex.appendingPathComponent("rollout-test.jsonl"))

        let result = try runUsageData(home: home)
        let rows = try JSONDecoder().decode([MeterRow].self, from: result)
        let session = try XCTUnwrap(rows.first { $0.label == "cl·pri" && $0.window == "5h" })
        let weekly = try XCTUnwrap(rows.first { $0.provider == "codex" })
        let offline = try XCTUnwrap(rows.first { $0.label == "cl·endu" })

        XCTAssertEqual(session.canFetch, true)
        XCTAssertEqual(weekly.canFetch, true)
        XCTAssertEqual(offline.state, "offline")
        XCTAssertEqual(offline.canFetch, true)
    }

    func testCodexAPICacheBeatsLowerRolloutValueInTheSameWindow() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let codexHome = home.appendingPathComponent(".codex")
        let sessions = codexHome.appendingPathComponent("sessions/2026/08/03")
        try FileManager.default.createDirectory(at: sessions, withIntermediateDirectories: true)
        try signIn(codexHome: codexHome)
        defer { try? FileManager.default.removeItem(at: home) }

        let now = Int(Date().timeIntervalSince1970)
        let reset = 4_102_444_800
        let rollout = """
        {"payload":{"rate_limits":{"primary":{"used_percent":42,"window_minutes":10080,"resets_at":\(reset)},"secondary":null}}}
        """
        let api = """
        {"rate_limits":{"primary":{"used_percent":64,"window_minutes":10080,"resets_at":\(reset)},"secondary":null},"fetched_at":\(now),"source":"api"}
        """
        try Data(rollout.utf8).write(to: sessions.appendingPathComponent("rollout-test.jsonl"))
        try Data(api.utf8).write(to: codexHome.appendingPathComponent(".usage-hud-api-cache.json"))

        let rows = try JSONDecoder().decode([MeterRow].self, from: try runUsageData(home: home))
        let codex = try XCTUnwrap(rows.first { $0.provider == "codex" })

        XCTAssertEqual(codex.pct, 64)
        XCTAssertEqual(codex.source, "api")
        XCTAssertEqual(codex.canFetch, true)
    }

    func testOfflineCodexRowCanBePopulatedByFetch() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let claude = home.appendingPathComponent(".claude/.profiles/pri")
        try FileManager.default.createDirectory(at: claude, withIntermediateDirectories: true)
        // A signed-in codex home with no cache and no rollout: the row the fetch fills in.
        try signIn(codexHome: home.appendingPathComponent(".codex"))
        defer { try? FileManager.default.removeItem(at: home) }

        let rows = try JSONDecoder().decode([MeterRow].self, from: try runUsageData(home: home))
        let codex = try XCTUnwrap(rows.first { $0.provider == "codex" })

        XCTAssertEqual(codex.state, "offline")
        XCTAssertEqual(codex.canFetch, true)
    }

    /// A codex home is only measured when it holds credentials — the provider reports one
    /// `logged_out` row otherwise. Real homes always carry this file; fixtures must too.
    private func signIn(codexHome: URL) throws {
        try FileManager.default.createDirectory(at: codexHome, withIntermediateDirectories: true)
        let auth = #"{"tokens":{"access_token":"fixture","refresh_token":"fixture"}}"#
        try Data(auth.utf8).write(to: codexHome.appendingPathComponent("auth.json"))
    }

    private func runUsageData(home: URL) throws -> Data {
        var environment = ProcessInfo.processInfo.environment
        environment["HOME"] = home.path
        environment["USAGE_HUD_STALE_AFTER"] = "9999999999"
        environment["CODEX_BIN"] = "/usr/bin/true"
        // The provider asks the Keychain whether each Claude profile is signed in, and a
        // fixture HOME has no Keychain item: without this the whole profile collapses to
        // one `logged_out` row. /usr/bin/true is the same pin the Python tests use.
        environment["AGENT_PROFILES_SECURITY_BIN"] = "/usr/bin/true"

        // The provider is whatever the app itself would run for a snapshot, resolved from
        // this same environment — so these cases exercise the shipped contract instead of
        // a path only the tests know. A machine without the provider installed skips
        // rather than fails: the contract under test is the app's, not the installation's.
        let command = processArguments(kind: .snapshot, environment: environment)
        guard FileManager.default.isExecutableFile(atPath: command.executable) else {
            throw XCTSkip("no usage provider at \(command.executable)")
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: command.executable)
        process.arguments = command.arguments
        process.environment = environment
        let stdout = Pipe()
        process.standardOutput = stdout
        process.standardError = Pipe()

        try process.run()
        let data = stdout.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, 0)
        return data
    }
}
