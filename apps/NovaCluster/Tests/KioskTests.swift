//
//  KioskTests.swift
//  Nova Cluster 1.1.0: kiosk mode timing (cycle the three views; a touch pauses it).
//

import XCTest

final class KioskPolicyTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_000_000)

    func testOffNeverAdvances() {
        let k = KioskPolicy(enabled: false, interval: 60)
        XCTAssertFalse(k.shouldAdvance(now: t0.addingTimeInterval(10_000), lastSwitch: t0, lastTouch: nil))
    }

    func testAdvancesAfterInterval() {
        let k = KioskPolicy(enabled: true, interval: 60)
        XCTAssertFalse(k.shouldAdvance(now: t0.addingTimeInterval(59), lastSwitch: t0, lastTouch: nil))
        XCTAssertTrue(k.shouldAdvance(now: t0.addingTimeInterval(60), lastSwitch: t0, lastTouch: nil))
    }

    func testTouchPausesForAtLeastTwoMinutes() {
        let k = KioskPolicy(enabled: true, interval: 30)
        let touch = t0.addingTimeInterval(20)
        XCTAssertFalse(k.shouldAdvance(now: touch.addingTimeInterval(119), lastSwitch: t0, lastTouch: touch))
        XCTAssertTrue(k.shouldAdvance(now: touch.addingTimeInterval(121), lastSwitch: t0, lastTouch: touch))
        // A long interval pauses for the whole interval.
        let slow = KioskPolicy(enabled: true, interval: 600)
        XCTAssertEqual(slow.pause, 600)
        XCTAssertFalse(slow.shouldAdvance(now: touch.addingTimeInterval(599), lastSwitch: t0, lastTouch: touch))
    }

    func testClampAndLoadDefaults() {
        XCTAssertEqual(KioskPolicy.clamp(1), 10)
        XCTAssertEqual(KioskPolicy.clamp(99_999), 3600)
        XCTAssertEqual(KioskPolicy.clamp(.nan), KioskPolicy.defaultInterval)
        let d = UserDefaults(suiteName: "kiosk-test-\(UUID())")!
        let k = KioskPolicy.load(d)
        XCTAssertEqual(k.enabled, KioskPolicy.defaultEnabled)
        XCTAssertEqual(k.interval, 60)
    }

    func testDeepLink() {
        let d = UserDefaults(suiteName: "kiosk-test-\(UUID())")!
        XCTAssertTrue(KioskPolicy.apply(url: URL(string: "novacluster://kiosk?on=1&interval=5")!, defaults: d))
        XCTAssertEqual(KioskPolicy.load(d), KioskPolicy(enabled: true, interval: 10))   // clamped
        XCTAssertTrue(KioskPolicy.apply(url: URL(string: "novacluster://kiosk?on=off")!, defaults: d))
        XCTAssertFalse(KioskPolicy.load(d).enabled)
        XCTAssertFalse(KioskPolicy.apply(url: URL(string: "novacluster://setup?hub=x")!, defaults: d))
    }
}
