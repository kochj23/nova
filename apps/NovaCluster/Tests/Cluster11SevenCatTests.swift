//
//  Cluster11SevenCatTests.swift
//  Nova Cluster 1.1.0 in the house's 7 categories (Security, Performance, Retry, Unit, Integration,
//  Functional, Frame), as extensions of the category classes in NodeLinesTests.swift.
//  Covers the constellation merge, kiosk, drill-down, UPS, alert strip, ticker and the menu-bar item.
//

import AppKit
import XCTest

enum Fixture11 {
    /// A /cluster payload in the hub's 1.1 shape: nodes with net/temps/recent_jobs, ups, alerts, published, lessons.
    static func clusterJSON(bad: Bool = true) -> Data {
        let nodes: [[String: Any]] = [
            ["node": "mac-studio", "live": true, "minion": ["state": "dedicated", "tonight": ["works": 384, "lessons": 791]],
             "net": ["default": "en0", "eth": ["iface": "en0", "up": true, "speed_mbps": 1000, "ip": "10.0.0.6"],
                     "wifi": ["iface": "en1", "up": true, "ip": "10.0.0.64"]]],
            ["node": "nova-core7", "live": true, "minion": ["state": "idle", "tonight": ["transcribed": 10, "audio_h": 5.6],
                                                            "now_reading": [["title": "Cannon — S03E15", "phase": "transcribing"]]],
             "net": ["default": "eno1", "eth": ["iface": "eno1", "up": true, "speed_mbps": 1000, "ip": "10.0.0.125"],
                     "wifi": ["iface": "wlp194s0", "up": true, "ip": "10.0.0.197"]],
             "temps": ["cpu": 52.4, "gpu": 43, "nvme": 33.9],
             "recent_jobs": [["at": "2026-10-19T20:45:00-07:00", "kind": "llm", "text": "nova_notifier @ mac-studio → qwen3:8b"],
                             ["at": "2026-10-19T17:38:00-07:00", "kind": "transcribing", "text": "CrashCourse — S27E0005"]]],
            ["node": "nova-core4", "live": true, "net": ["default": "eno1", "eth": ["iface": "eno1", "up": true, "speed_mbps": 1000]]],
        ]
        let alerts: [[String: Any]] = bad ? [
            ["level": "bad", "kind": "quarantined", "source": "studio", "task": "model_warm", "text": "model_warm: quarantined after 10 failures in a row"],
            ["level": "warn", "kind": "failing", "source": "scheduler-core", "task": "x", "text": "x: failed 3× in a row", "failures": 3],
            ["level": "info", "kind": "leader", "source": "scheduler-core", "text": "leader → nova-core · Mon 17:59"]] : []
        let body: [String: Any] = [
            "nodes": nodes, "events": [],
            "recent_lessons": [["title": "Dune", "lesson": "Fear is the mind-killer [3].", "node": "mac-studio"],
                               ["title": "Dune", "lesson": "Spice must flow.", "node": "nova-core7"]],
            "ups": [["key": "master_bedroom", "label": "Master Bedroom", "powers": ["nova-core4", "nova-core7"], "ok": true,
                     "online": true, "status": "OL", "load_pct": 18, "watts": 162, "charge_pct": 100, "runtime_s": 1764]],
            "alerts": ["items": alerts, "counts": ["bad": bad ? 1 : 0, "warn": bad ? 1 : 0, "info": bad ? 1 : 0], "leader": "nova-core"],
            "published": [["title": "Nobody Ever Asked HAL How He Was Feeling", "section": "nova", "emoji": "🪞",
                           "url": "https://nova.digitalnoise.net/nova/2026-10-19-nobody-ever-asked-hal/", "at": "2026-10-20T01:51:00Z"]],
        ]
        return try! JSONSerialization.data(withJSONObject: body)
    }

    static func snapshot(bad: Bool = true) -> ClusterSnapshot {
        try! JSONDecoder().decode(ClusterSnapshot.self, from: clusterJSON(bad: bad))
    }
}

// MARK: - Security

extension SecurityTests {
    func testV11DeepLinksValidateTheirInput() {
        XCTAssertNil(ClusterConfig.nodeLink(url: URL(string: "novacluster://node?name=../../etc")!))
        XCTAssertNil(ClusterConfig.nodeLink(url: URL(string: "otherapp://node?name=nova-core7")!))
        XCTAssertFalse(ClusterConfig.applyView(url: URL(string: "novacluster://view?mode=%3Cscript%3E")!))
        let d = UserDefaults(suiteName: "sec-\(UUID())")!
        KioskPolicy.apply(url: URL(string: "novacluster://kiosk?interval=-5")!, defaults: d)
        XCTAssertEqual(KioskPolicy.load(d).interval, KioskPolicy.defaultInterval, "a non-positive interval falls back to the default")
    }

    func testOnlyNovasOwnSiteIsEverOpened() {
        for bad in ["http://nova.digitalnoise.net/x", "https://nova.digitalnoise.net.evil.example/x", "file:///etc/passwd", "javascript:alert(1)"] {
            XCTAssertNil(PublishedText.link(.init(title: "t", url: bad)), bad)
        }
    }

    func testHubAlertTextCannotSpoofTheStrip() {
        let item = ClusterSnapshot.Alerts.Item(level: "bad", source: "studio\u{202E}evil", text: "x\u{0}\ny" + String(repeating: "z", count: 900))
        let line = NodeLines.clean(AlertText.line(item), max: 90)
        XCTAssertFalse(line.contains("\u{202E}"))
        XCTAssertFalse(line.contains("\n"))
        XCTAssertLessThanOrEqual(line.count, 91)
    }
}

// MARK: - Performance

extension PerformanceTests {
    @MainActor
    func testMenuBarSummaryChangesAtMostOncePerPoll() {
        let st = MenuBarStatus()
        var snap = Fixture11.snapshot()
        for poll in 0..<200 {
            snap.nodes?[0].tok_s = Double(poll)               // traffic changes every poll
            st.update(snap, offline: false)
        }
        XCTAssertEqual(st.changes, 1, "200 polls of traffic must not touch the menu bar")
    }

    func testConstellationPlacementIsCheap() {
        measure {
            for i in 0..<5000 { _ = ConstellationLayout.place(work: "w\(i % 40)", key: "k\(i)", aspect: 1.6) }
        }
    }

    func testDecodingTheV11PayloadStaysFast() {
        let data = Fixture11.clusterJSON()
        let t0 = Date()
        for _ in 0..<500 { _ = try? JSONDecoder().decode(ClusterSnapshot.self, from: data) }
        XCTAssertLessThan(Date().timeIntervalSince(t0), 2.0)
    }
}

// MARK: - Retry

extension RetryTests {
    @MainActor
    func testMenuBarShowsHubDownThenRecovers() async {
        StubProtocol.responses = [(200, Fixture11.clusterJSON()), (503, Data()), (503, Data()), (200, Fixture11.clusterJSON())]
        let (m, _, suite) = makeModel()
        defer { UserDefaults().removePersistentDomain(forName: suite) }
        await m.pollOnce()
        XCTAssertEqual(m.menuStatus.health.title, "3/3 · 2!")
        await m.pollOnce(); await m.pollOnce()
        XCTAssertEqual(m.menuStatus.health.title, "hub?")
        XCTAssertEqual(m.snapshot?.ups?.first?.key, "master_bedroom", "the last UPS reading stays on screen")
        await m.pollOnce()
        XCTAssertEqual(m.menuStatus.health.title, "3/3 · 2!")
    }

    func testUnreachableUPSIsAmberNotAnError() {
        let u = ClusterSnapshot.UPS(key: "mb", label: "Master Bedroom", ok: false, error: "ConnectionRefusedError: x")
        XCTAssertEqual(NodeFacts.upsSummary(u).1, .warn)
        XCTAssertEqual(FleetHealth(Fixture11.snapshot(bad: false)).level, .good)
    }
}

// MARK: - Unit

extension UnitTests {
    func testViewModeCycleAndSubtitles() {
        XCTAssertEqual(ViewMode.allCases.map(\.next), [.dashboard, .constellation, .map])
        XCTAssertEqual(ViewMode.allCases.map(\.subtitle), ["· CLUSTER", "· DASHBOARD", "· MINION"])
    }

    func testLinkAndBackdoorWording() {
        let n = Fixture11.snapshot().allNodes[1]
        XCTAssertEqual(NodeFacts.ethernet(n.net).0, "eno1 · up · 1 Gb/s · 10.0.0.125")
        XCTAssertEqual(NodeFacts.backdoor(n.net).0, "wlp194s0 · up · 10.0.0.197")
        XCTAssertEqual(NodeFacts.backdoor(Fixture11.snapshot().allNodes[2].net).1, .warn)   // nova-core4: no Wi-Fi
    }

    func testMinionCardWording() {
        let n = Fixture11.snapshot().allNodes[1]
        XCTAssertEqual(ConstellationFleet.tonightParts(n.minion?.tonight), ["10 episodes transcribed", "5.6 h audio"])
        XCTAssertEqual(ConstellationFleet.readingText(n.minion!.now_reading![0]), "transcribing: Cannon — S03E15")
    }
}

// MARK: - Integration

extension IntegrationTests {
    @MainActor
    func testHubPayloadFlowsToEverySurface() async {
        StubProtocol.responses = [(200, Fixture11.clusterJSON())]
        let (m, _, suite) = makeModel()
        defer { UserDefaults().removePersistentDomain(forName: suite) }
        await m.pollOnce()
        let s = try! XCTUnwrap(m.snapshot)
        XCTAssertEqual(ConstellationFleet.minions(s).map(\.node), ["mac-studio", "nova-core7"])
        XCTAssertEqual(NodeFacts.ups(for: "nova-core7", in: s.ups)?.label, "Master Bedroom")
        XCTAssertNil(NodeFacts.ups(for: "mac-studio", in: s.ups))
        XCTAssertEqual(AlertText.headline(s.alerts), "1 dead-letter · 1 failing")
        XCTAssertEqual(m.menuStatus.latest?.url.host, "nova.digitalnoise.net")
        XCTAssertEqual(LessonText.clean(s.lessons[0].lesson ?? ""), "Fear is the mind-killer.")
        XCTAssertEqual(NodeFacts.temps(s.allNodes[1].temps)?.0, "CPU 52 °C · GPU 43 °C · NVMe 34 °C")
    }
}

// MARK: - Functional

extension FunctionalTests {
    func testKioskVisitsAllThreeViewsThenPausesOnTouch() {
        let k = KioskPolicy(enabled: true, interval: 60)
        var mode = ViewMode.map, lastSwitch = Date(timeIntervalSince1970: 0)
        var visited: [ViewMode] = [mode]
        for s in stride(from: 1.0, through: 200.0, by: 1.0) {
            let now = Date(timeIntervalSince1970: s)
            if k.shouldAdvance(now: now, lastSwitch: lastSwitch, lastTouch: nil) { mode = mode.next; lastSwitch = now; visited.append(mode) }
        }
        XCTAssertEqual(visited, [.map, .dashboard, .constellation, .map])
        let touch = Date(timeIntervalSince1970: 300)
        XCTAssertFalse(k.shouldAdvance(now: Date(timeIntervalSince1970: 400), lastSwitch: lastSwitch, lastTouch: touch))
    }

    func testDrillDownDeepLinkAndViewSwitch() {
        XCTAssertEqual(ClusterConfig.nodeLink(url: URL(string: "novacluster://node?name=nova-core7")!), "nova-core7")
        let d = UserDefaults.standard, before = d.string(forKey: ViewMode.key)
        defer { d.set(before, forKey: ViewMode.key) }
        for m in ViewMode.allCases {
            XCTAssertTrue(ClusterConfig.applyView(url: URL(string: "novacluster://view?mode=\(m.rawValue)")!))
            XCTAssertEqual(ViewMode(stored: d.string(forKey: ViewMode.key)), m)
        }
    }

    func testMenuLinesForAProblemFleet() {
        let h = FleetHealth(Fixture11.snapshot())
        XCTAssertEqual(h.lines.map(\.0).prefix(2), ["3 of 3 machines live", "2 scheduler alerts (1 red, 1 amber)"])
    }
}

// MARK: - Frame

extension FrameTests {
    /// The menu-bar item is AppKit, outside SwiftUI's scene graph, and it appears / disappears with the setting.
    @MainActor
    func testStatusItemFollowsTheSetting() {
        let d = UserDefaults.standard
        let before = d.object(forKey: StatusItemController.menuBarKey)
        defer { d.set(before, forKey: StatusItemController.menuBarKey) }
        d.set(true, forKey: StatusItemController.menuBarKey)
        let status = MenuBarStatus()
        StatusItemController.shared.attach(status) {}
        XCTAssertTrue(StatusItemController.shared.isShowing)
        status.update(Fixture11.snapshot(), offline: false)              // re-render on change, no crash
        d.set(false, forKey: StatusItemController.menuBarKey)
        NotificationCenter.default.post(name: UserDefaults.didChangeNotification, object: d)
        let gone = expectation(description: "hidden"); DispatchQueue.main.async { gone.fulfill() }
        wait(for: [gone], timeout: 2)
        XCTAssertFalse(StatusItemController.shared.isShowing)
    }

    /// Cold launch: a cached payload is on screen before the first poll; start/stop never blocks.
    @MainActor
    func testColdLaunchFromCacheAndCleanStartStop() {
        let suite = "frame-\(UUID())"
        let d = UserDefaults(suiteName: suite)!
        defer { d.removePersistentDomain(forName: suite) }
        d.set(Fixture11.clusterJSON(), forKey: ClusterModel.cacheKey)
        let m = ClusterModel(defaults: d, session: StubProtocol.session(),
                             config: { ClusterConfig(hub: "http://hub.invalid:1", node: "t", token: "") })
        XCTAssertEqual(m.snapshot?.ups?.count, 1)
        let t0 = Date()
        m.start(); m.stop(); m.start(); m.stop()
        XCTAssertLessThan(Date().timeIntervalSince(t0), 0.5)
    }
}
