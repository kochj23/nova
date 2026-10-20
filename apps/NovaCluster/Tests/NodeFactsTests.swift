//
//  NodeFactsTests.swift
//  Nova Cluster 1.1.0: the machine drill-down's wording (link, Wi-Fi backdoor, temps, UPS) and
//  decoding the hub's new /cluster fields.
//

import XCTest

final class NodeFactsTests: XCTestCase {
    typealias Net = ClusterSnapshot.Net

    func testSpeeds() {
        XCTAssertEqual(NodeFacts.speed(1000), "1 Gb/s")
        XCTAssertEqual(NodeFacts.speed(2500), "2.5 Gb/s")
        XCTAssertEqual(NodeFacts.speed(100), "100 Mb/s")
        XCTAssertNil(NodeFacts.speed(0))
    }

    func testEthernetAndBackdoor() {
        let net = Net(default: "eno1", eth: .init(iface: "eno1", up: true, speed_mbps: 1000, ip: "192.168.1.125"),
                      wifi: .init(iface: "wlp194s0", up: true, ip: "192.168.1.197"))
        XCTAssertEqual(NodeFacts.ethernet(net).0, "eno1 · up · 1 Gb/s · 192.168.1.125")
        XCTAssertEqual(NodeFacts.ethernet(net).1, .good)
        XCTAssertEqual(NodeFacts.backdoor(net).0, "wlp194s0 · up · 192.168.1.197")
        XCTAssertEqual(NodeFacts.route(net)?.1, .quiet)
        // nova-core4: no Wi-Fi at all
        XCTAssertEqual(NodeFacts.backdoor(Net(eth: net.eth, wifi: nil)).1, .warn)
        // the cable dropped: wired DOWN, traffic over Wi-Fi
        let dropped = Net(default: "wlp194s0", eth: .init(iface: "eno1", up: false), wifi: net.wifi)
        XCTAssertEqual(NodeFacts.ethernet(dropped).1, .bad)
        XCTAssertEqual(NodeFacts.route(dropped)?.1, .warn)
        XCTAssertEqual(NodeFacts.ethernet(nil).1, .quiet)
    }

    func testTemps() {
        XCTAssertEqual(NodeFacts.temps(["gpu": 43, "cpu": 52.4, "nvme": 33.9])?.0, "CPU 52 °C · GPU 43 °C · NVMe 34 °C")
        XCTAssertEqual(NodeFacts.temps(["cpu": 91])?.1, .warn)
        XCTAssertNil(NodeFacts.temps(nil))
        XCTAssertNil(NodeFacts.temps([:]))
    }

    func testRuntime() {
        XCTAssertEqual(NodeFacts.runtime(45), "45 s")
        XCTAssertEqual(NodeFacts.runtime(1764), "29 min")
        XCTAssertEqual(NodeFacts.runtime(3900), "1 h 05 min")
        XCTAssertNil(NodeFacts.runtime(nil))
    }

    func testUPS() {
        var u = ClusterSnapshot.UPS(key: "master_bedroom", label: "Master Bedroom", powers: ["nova-core4", "nova-core7"],
                                    ok: true, status: "OL", on_battery: false, low_battery: false, online: true,
                                    load_pct: 18, watts: 162, charge_pct: 100, runtime_s: 1764)
        XCTAssertEqual(NodeFacts.upsSummary(u).0, "18% load · 162 W · 100% · 29 min · on line")
        XCTAssertEqual(NodeFacts.upsSummary(u).1, .good)
        XCTAssertEqual(NodeFacts.ups(for: "nova-core7", in: [u])?.key, "master_bedroom")
        XCTAssertNil(NodeFacts.ups(for: "mac-studio", in: [u]))
        u.on_battery = true; u.online = false
        XCTAssertEqual(NodeFacts.upsSummary(u).1, .bad)
        XCTAssertTrue(NodeFacts.upsSummary(u).0.hasSuffix("ON BATTERY"))
        u.low_battery = true
        XCTAssertTrue(NodeFacts.upsSummary(u).0.hasSuffix("LOW BATTERY"))
        let dark = ClusterSnapshot.UPS(key: "x", ok: false, error: "ConnectionRefusedError: x")
        XCTAssertEqual(NodeFacts.upsSummary(dark).1, .warn)
    }

    func testNodeDeepLink() {
        XCTAssertEqual(ClusterConfig.nodeLink(url: URL(string: "novacluster://node?name=nova-core7")!), "nova-core7")
        XCTAssertNil(ClusterConfig.nodeLink(url: URL(string: "novacluster://node?name=../etc")!))
        XCTAssertNil(ClusterConfig.nodeLink(url: URL(string: "novacluster://view?mode=map")!))
    }

    func testDecodesHubExtras() throws {
        let json = """
        {"nodes":[{"node":"nova-core7","net":{"default":"eno1","eth":{"iface":"eno1","up":true,"speed_mbps":1000,"ip":"192.168.1.125"},
          "wifi":{"iface":"wlp194s0","up":true,"ip":"192.168.1.197"}},"temps":{"cpu":52.4},
          "recent_jobs":[{"at":"2026-10-19T20:40:29-07:00","kind":"transcribing","text":"Cannon"}]}],
         "ups":[{"key":"master_bedroom","label":"Master Bedroom","powers":["nova-core7"],"ok":true,"online":true,"load_pct":18}]}
        """
        let s = try JSONDecoder().decode(ClusterSnapshot.self, from: Data(json.utf8))
        let n = try XCTUnwrap(s.allNodes.first)
        XCTAssertEqual(n.net?.wifi?.ip, "192.168.1.197")
        XCTAssertEqual(n.temps?["cpu"], 52.4)
        XCTAssertEqual(n.recent_jobs?.first?.kind, "transcribing")
        XCTAssertEqual(s.ups?.first?.load_pct, 18)
        // an older hub without the new fields still decodes
        XCTAssertNoThrow(try JSONDecoder().decode(ClusterSnapshot.self, from: Data(#"{"nodes":[{"node":"a"}]}"#.utf8)))
    }
}

final class AlertTextTests: XCTestCase {
    typealias A = ClusterSnapshot.Alerts

    func testQuietWhenHealthy() {
        let a = A(items: [.init(level: "info", kind: "leader", source: "scheduler-core", text: "leader → nova-core · Mon 17:59")])
        XCTAssertNil(AlertText.headline(a))
        XCTAssertNil(AlertText.headline(nil))
        XCTAssertEqual(AlertText.level(a), .good)
    }

    func testHeadlineAndLevel() {
        let a = A(items: [.init(level: "bad", kind: "quarantined", source: "studio", task: "model_warm", text: "model_warm: quarantined"),
                          .init(level: "bad", kind: "dead-letter", source: "studio", text: "x"),
                          .init(level: "warn", kind: "failing", source: "scheduler-core", text: "y"),
                          .init(level: "warn", kind: "failover", source: "scheduler-core", text: "on standby"),
                          .init(level: "info", kind: "leader", text: "z")])
        XCTAssertEqual(AlertText.headline(a), "2 dead-letter · 1 failing · failover")
        XCTAssertEqual(AlertText.level(a), .bad)
        XCTAssertEqual(AlertText.problems(a).count, 4)
        XCTAssertEqual(AlertText.line(a.items![0]), "studio · model_warm: quarantined")
    }

    func testDecodesAlerts() throws {
        let json = #"{"alerts":{"items":[{"level":"warn","kind":"failing","source":"studio","task":"t","text":"t: failed 4× in a row","failures":4}],"counts":{"bad":0,"warn":1},"leader":"nova-core"}}"#
        let s = try JSONDecoder().decode(ClusterSnapshot.self, from: Data(json.utf8))
        XCTAssertEqual(s.alerts?.leader, "nova-core")
        XCTAssertEqual(AlertText.headline(s.alerts), "1 failing")
    }
}

final class PublishedTextTests: XCTestCase {
    func testOnlyNovaSiteLinksOpen() {
        let ok = ClusterSnapshot.Post(title: "T", url: "https://nova.digitalnoise.net/belief/a/")
        XCTAssertNotNil(PublishedText.link(ok))
        XCTAssertNil(PublishedText.link(.init(title: "T", url: "https://evil.example/belief/a/")))
        XCTAssertNil(PublishedText.link(.init(title: "T", url: "javascript:alert(1)")))
        XCTAssertNil(PublishedText.link(.init(title: "T", url: nil)))
    }

    func testLine() {
        let p = ClusterSnapshot.Post(title: "Belief and Friends: Garments", emoji: "🔍", at: "2026-10-20T02:13:02Z")
        XCTAssertEqual(PublishedText.line(p, clock: { _ in "19:13" }), "🔍 Belief and Friends: Garments · 19:13")
        XCTAssertEqual(PublishedText.line(.init(title: nil, emoji: nil), clock: { _ in "" }), "• untitled")
    }
}

final class FleetHealthTests: XCTestCase {
    private func snap(live: Int, of total: Int, alerts: [ClusterSnapshot.Alerts.Item] = [], ups: [ClusterSnapshot.UPS] = []) -> ClusterSnapshot {
        var s = ClusterSnapshot()
        s.nodes = (0..<total).map { ClusterSnapshot.Node(node: "n\($0)", live: $0 < live) }
        s.alerts = .init(items: alerts)
        s.ups = ups
        return s
    }

    func testAllGood() {
        let ok = ClusterSnapshot.UPS(key: "mb", label: "Master Bedroom", ok: true, online: true, load_pct: 18, charge_pct: 100, runtime_s: 1764)
        let h = FleetHealth(snap(live: 10, of: 10, ups: [ok]))
        XCTAssertEqual(h.title, "10/10")
        XCTAssertEqual(h.level, .good)
        XCTAssertEqual(h.lines.map(\.0).prefix(2), ["10 of 10 machines live", "schedulers healthy"])
        XCTAssertTrue(h.lines[2].0.hasPrefix("Master Bedroom UPS · 18% load"))
    }

    func testProblems() {
        let h = FleetHealth(snap(live: 9, of: 10, alerts: [.init(level: "bad", kind: "quarantined"), .init(level: "warn", kind: "failing"),
                                                           .init(level: "info", kind: "leader")]))
        XCTAssertEqual(h.title, "9/10 · 2!")
        XCTAssertEqual(h.level, .bad)
        XCTAssertEqual(h.lines[1].0, "2 scheduler alerts (1 red, 1 amber)")
    }

    func testOnBatteryAndOffline() {
        let ob = ClusterSnapshot.UPS(key: "mb", ok: true, on_battery: true, online: false)
        XCTAssertEqual(FleetHealth(snap(live: 2, of: 2, ups: [ob])).level, .bad)
        let off = FleetHealth(nil, offline: true)
        XCTAssertEqual(off.title, "hub?")
        XCTAssertEqual(off.level, .bad)
        XCTAssertEqual(off.lines.first?.0, "Nova's hub is not answering")
    }
}

@MainActor
final class MenuBarStatusTests: XCTestCase {
    func testOnlyChangesWhenTheSummaryChanges() {
        let st = MenuBarStatus()
        var s = ClusterSnapshot()
        s.nodes = [ClusterSnapshot.Node(node: "a", live: true), ClusterSnapshot.Node(node: "b", live: true)]
        s.published = [.init(title: "T", emoji: "✨", url: "https://nova.digitalnoise.net/nova/t/")]
        st.update(s, offline: false)
        let first = st.health
        XCTAssertEqual(first.title, "2/2")
        XCTAssertEqual(st.latest?.url.absoluteString, "https://nova.digitalnoise.net/nova/t/")
        // tok/s, events, lessons — the fast-changing parts — don't touch the summary
        s.events = [.init(node: "a", model: "m", prompt: 1, decoded: 2)]
        s.nodes?[0].tok_s = 99
        st.update(s, offline: false)
        XCTAssertEqual(st.health, first)
        s.nodes?[1].live = false
        st.update(s, offline: false)
        XCTAssertEqual(st.health.title, "1/2")
        st.update(s, offline: true)
        XCTAssertEqual(st.health.title, "hub?")
    }

    /// Performance: over a simulated 40 s (20 polls) of traffic, kiosk switches and constellation lessons, the
    /// menu-bar summary must change no more often than real health changes (here: once, at the first poll).
    func testChangeRateIsAtMostPollRateAndOnlyOnRealChanges() {
        let st = MenuBarStatus()
        var fired = 0
        st.onChange = { fired += 1 }
        var s = ClusterSnapshot()
        s.nodes = (0..<10).map { ClusterSnapshot.Node(node: "n\($0)", live: true) }
        for poll in 0..<20 {
            s.nodes?[poll % 10].tok_s = Double(poll * 7)                       // traffic changes every poll
            s.events = [.init(node: "n1", model: "m", prompt: Double(poll), decoded: 1)]
            s.recent_lessons = [.init(title: "w\(poll)", lesson: "l\(poll)")]  // constellation churn
            UserDefaults.standard.set(ViewMode.allCases[poll % 3].rawValue, forKey: "menu-test-mode")   // kiosk ticks
            st.update(s, offline: false)
        }
        XCTAssertEqual(fired, 1)
        XCTAssertEqual(st.changes, 1)
        XCTAssertLessThanOrEqual(st.changes, 20)
    }
}
