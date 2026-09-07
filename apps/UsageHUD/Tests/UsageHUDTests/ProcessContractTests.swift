import Foundation
import XCTest
@testable import UsageHUD

final class ProcessContractTests: XCTestCase {
    func testSnapshotRunsOnlyLocalUsageRead() {
        let command = snapshotArguments(environment: ["JELLO_BIN": "/opt/bin/jello"])
        XCTAssertEqual(command.executable, "/opt/bin/jello")
        XCTAssertEqual(command.arguments, ["usage", "show", "--json"])
    }

    func testDataFixtureKeepsTheJSONContract() {
        let command = snapshotArguments(environment: ["USAGE_HUD_SCRIPT": "/tmp/data"])
        XCTAssertEqual(command.executable, "/tmp/data")
        XCTAssertEqual(command.arguments, ["--json"])
    }

    func testOldFetchOverrideCannotStartAnOnlineProcess() {
        let command = snapshotArguments(environment: [
            "JELLO_BIN": "/opt/bin/jello",
            "USAGE_HUD_FETCH_SCRIPT": "/tmp/online-fetch",
        ])
        XCTAssertEqual(command.executable, "/opt/bin/jello")
        XCTAssertEqual(command.arguments, ["usage", "show", "--json"])
    }
}
