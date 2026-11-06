//
//  PhoneLayoutTests.swift — Nova Cluster
//
//  iPhone layout (1.2.0): off a phone the helpers must leave the iPad / Mac / TV layouts exactly as they were.
//

import XCTest

final class PhoneLayoutTests: XCTestCase {
    func testNotAPhoneHere() {
        XCTAssertFalse(DeviceLayout.isPhone)
    }

    func testUnitMatchesTheIPadFormula() {
        XCTAssertEqual(DeviceLayout.unit(CGSize(width: 1180, height: 820)), 1.0, accuracy: 0.001)
        XCTAssertEqual(DeviceLayout.unit(CGSize(width: 600, height: 400)), 0.8, accuracy: 0.001)   // floor
        XCTAssertEqual(DeviceLayout.unit(CGSize(width: 1366, height: 1024)), 1024.0 / 820, accuracy: 0.001)
    }

    func testPhoneUnitIsReadable() {
        XCTAssertGreaterThanOrEqual(DeviceLayout.phoneUnit, 0.55)
        XCTAssertLessThan(DeviceLayout.phoneUnit, 0.8)
    }

    func testColumnsAndStripPadUnchangedOffPhone() {
        XCTAssertEqual(DeviceLayout.columns(pad: 4), 4)
        XCTAssertEqual(DeviceLayout.columns(pad: 3), 3)
        XCTAssertEqual(DeviceLayout.stripPad(nil), 0)
    }
}
