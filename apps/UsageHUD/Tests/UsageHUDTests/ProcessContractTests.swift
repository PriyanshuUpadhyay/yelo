import Foundation
import XCTest
@testable import UsageHUD

/// C15: what the app runs, and with which arguments, for each of its two subprocess jobs.
///
/// The default is jello, located by the absolute path the LaunchAgent supplies, because
/// launchd gives the job no PATH to search. Each script override keeps the contract the
/// fixture scripts were written against, and neither override reaches the other job.
final class ProcessContractTests: XCTestCase {
    private let defaultJello = ("~/.local/bin/jello" as NSString).expandingTildeInPath

    func testSnapshotWithoutOverridesRunsJelloUsageShow() {
        let command = processArguments(kind: .snapshot, environment: [:])

        XCTAssertEqual(command.executable, defaultJello)
        XCTAssertEqual(command.arguments, ["usage", "show", "--json"])
    }

    func testSnapshotUsesTheLaunchAgentsAbsoluteJelloPath() {
        let command = processArguments(
            kind: .snapshot,
            environment: ["JELLO_BIN": "/opt/bin/jello"]
        )

        XCTAssertEqual(command.executable, "/opt/bin/jello")
        XCTAssertEqual(command.arguments, ["usage", "show", "--json"])
    }

    func testDataScriptOverrideKeepsThePathPlusJSONContract() {
        let command = processArguments(
            kind: .snapshot,
            environment: ["USAGE_HUD_SCRIPT": "/tmp/data", "JELLO_BIN": "/opt/bin/jello"]
        )

        XCTAssertEqual(command.executable, "/tmp/data")
        XCTAssertEqual(command.arguments, ["--json"])
    }

    func testFetchWithoutOverridesRunsJelloUsageFetch() {
        let command = processArguments(kind: .fetch, environment: [:])

        XCTAssertEqual(command.executable, defaultJello)
        XCTAssertEqual(command.arguments, ["usage", "fetch"])
    }

    func testFetchUsesTheLaunchAgentsAbsoluteJelloPath() {
        let command = processArguments(
            kind: .fetch,
            environment: ["JELLO_BIN": "/opt/bin/jello"]
        )

        XCTAssertEqual(command.executable, "/opt/bin/jello")
        XCTAssertEqual(command.arguments, ["usage", "fetch"])
    }

    func testFetchScriptOverrideKeepsThePathAloneContract() {
        let command = processArguments(
            kind: .fetch,
            environment: ["USAGE_HUD_FETCH_SCRIPT": "/tmp/fetch"]
        )

        XCTAssertEqual(command.executable, "/tmp/fetch")
        XCTAssertTrue(command.arguments.isEmpty)
    }

    /// Each override belongs to one job. A fixture that replaces the data feed must not
    /// silently replace the fetch as well, or a fixture run would fire real API requests.
    func testEachOverrideIsScopedToItsOwnKind() {
        let environment = [
            "USAGE_HUD_SCRIPT": "/tmp/data",
            "USAGE_HUD_FETCH_SCRIPT": "/tmp/fetch",
            "JELLO_BIN": "/opt/bin/jello",
        ]

        XCTAssertEqual(processArguments(kind: .snapshot, environment: environment).executable,
                       "/tmp/data")
        XCTAssertEqual(processArguments(kind: .fetch, environment: environment).executable,
                       "/tmp/fetch")

        let dataOnly = ["USAGE_HUD_SCRIPT": "/tmp/data", "JELLO_BIN": "/opt/bin/jello"]
        XCTAssertEqual(processArguments(kind: .fetch, environment: dataOnly).executable,
                       "/opt/bin/jello")
        XCTAssertEqual(processArguments(kind: .fetch, environment: dataOnly).arguments,
                       ["usage", "fetch"])

        let fetchOnly = ["USAGE_HUD_FETCH_SCRIPT": "/tmp/fetch", "JELLO_BIN": "/opt/bin/jello"]
        XCTAssertEqual(processArguments(kind: .snapshot, environment: fetchOnly).executable,
                       "/opt/bin/jello")
        XCTAssertEqual(processArguments(kind: .snapshot, environment: fetchOnly).arguments,
                       ["usage", "show", "--json"])
    }
}
