import XCTest
@testable import UsageHUD

final class PressureParsingTests: XCTestCase {
    func testMinutesOnly() { XCTAssertEqual(parseResetHours("17m")!, 17.0 / 60.0, accuracy: 1e-9) }
    func testHoursMinutes() { XCTAssertEqual(parseResetHours("4h37m")!, 4 + 37.0 / 60.0, accuracy: 1e-9) }
    func testDaysHours() { XCTAssertEqual(parseResetHours("2d5h")!, 2 * 24 + 5, accuracy: 1e-9) }
    func testHoursOnly() { XCTAssertEqual(parseResetHours("12h")!, 12, accuracy: 1e-9) }
    func testNow() { XCTAssertEqual(parseResetHours("now"), 0) }
    func testEmpty() { XCTAssertEqual(parseResetHours(""), 0) }
    func testGarbage() { XCTAssertNil(parseResetHours("soon")) }
    func testTrailingUnitlessDigits() { XCTAssertNil(parseResetHours("4h37")) }
}
