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
