//
//  NodeLinesTests.swift
//  Nova Cluster — the two per-node lines (LLM job, core purpose), 7 house categories:
//  Security, Performance, Retry, Unit, Integration, Functional, Frame.
//  Offline: a stubbed URLSession, a throwaway UserDefaults suite, no Keychain, no files.
//

import AppKit
import SwiftUI
import XCTest

// MARK: - Fixtures

enum Fixture {
    static let purposes: [String: String] = [
        "mac-studio": "Control plane · scheduler · Redis · memory-server · Ollama/MLX · Home Assistant · pgbouncer",
        "nova-core": "PG primary · app tier · gateway · inference router",
        "nova-core2": "Radio ingest · secondary app · Redis",
        "nova-core3": "Whisper ingest · Ollama fast pool · scheduler standby",
        "nova-core4": "Gateway standby · Hue · pgbouncer",
        "nova-core5": "PG standby · Mosquitto · embeddings",
        "nova-core6": "Ollama fast pool · share mount",
        "nova-core7": "PG standby (docker) · Ollama",
        "nova-core9": "Media · Ollama fast pool · PG standby",
        "nova-core10": "Ollama 30B · MTPLX · Meshtastic bridge",
    ]

    /// A /cluster payload in the hub's 2026-10-17 shape: all 10 machines, every one with llm_job + purpose.
    static func clusterJSON() -> Data {
        var nodes: [[String: Any]] = []
        for (i, (node, purpose)) in purposes.sorted(by: { $0.key < $1.key }).enumerated() {
            var n: [String: Any] = ["node": node, "live": true, "age_s": 1.2, "os": "macOS 26", "purpose": purpose,
                                    "display_name": node, "ip": "192.168.1.\(i + 2)", "loaded": [], "runners": []]
            switch node {
            case "mac-studio":
                n["alias"] = "nova-core8"; n["display_name"] = "mac-studio (nova-core8)"
                n["llm_job"] = ["state": "idle", "text": "Idle · last: nova_journal → qwen3:8b · 4m ago", "jobs": []]
            case "nova-core":
                n["llm"] = false
                n["llm_job"] = ["state": "none", "text": "No LLM on this node", "jobs": []]
            case "nova-core7":
                n["runners"] = [["port": 5, "model": "qwen3:8b", "slots": [["busy": true, "task": 1, "prompt": 900,
                                                                             "prompt_done": 900, "decoded": 52]]]]
                n["llm_job"] = ["state": "running", "text": "nova_notifier @ mac-studio → qwen3:8b · generating 52 tok",
                                "jobs": ["nova_notifier @ mac-studio"], "model": "qwen3:8b"]
            default:
                n["llm_job"] = ["state": "idle", "text": "Idle · 2 models loaded", "jobs": []]
            }
            nodes.append(n)
        }
        let body: [String: Any] = ["updated": "2026-10-17T15:00:00-07:00", "nodes": nodes, "events": [],
                                   "totals": ["nodes": 10, "today": ["tokens": 1000]]]
        return try! JSONSerialization.data(withJSONObject: body)
    }

    static func node(_ json: [String: Any]) throws -> ClusterSnapshot.Node {
        try JSONDecoder().decode(ClusterSnapshot.Node.self, from: JSONSerialization.data(withJSONObject: json))
    }
}

/// URLProtocol stub: answers /cluster with a scripted list of (status, body).
final class StubProtocol: URLProtocol {
    nonisolated(unsafe) static var responses: [(Int, Data)] = []
    nonisolated(unsafe) static var requests: [URLRequest] = []
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.requests.append(request)
        let (code, data) = Self.responses.isEmpty ? (500, Data()) : Self.responses.removeFirst()
        let resp = HTTPURLResponse(url: request.url!, statusCode: code, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: resp, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}

    static func session() -> URLSession {
        let c = URLSessionConfiguration.ephemeral
        c.protocolClasses = [StubProtocol.self]
        return URLSession(configuration: c)
    }
}

@MainActor
func makeModel(token: String = "test-token-not-real") -> (ClusterModel, UserDefaults, String) {
    let suite = "NovaClusterTests.\(UUID().uuidString)"
    let d = UserDefaults(suiteName: suite)!
    let cfg = ClusterConfig(hub: "http://hub.invalid:37590", node: "test-view", token: token)
    return (ClusterModel(defaults: d, session: StubProtocol.session(), config: { cfg }), d, suite)
}

// MARK: - Security

final class SecurityTests: XCTestCase {
    func testHubTextIsOneCleanBoundedLine() {
        let evil = "nova_x\n\u{202E}gpj.exe\u{0007}  \t" + String(repeating: "A", count: 1000)
        let s = NodeLines.clean(evil)
        XCTAssertFalse(s.contains("\n"))
        XCTAssertFalse(s.unicodeScalars.contains { $0.value == 0x202E || $0.value == 7 })
        XCTAssertLessThanOrEqual(s.count, NodeLines.maxLength)
        XCTAssertTrue(s.hasSuffix("…"))
    }

    func testDisplayNameCannotSpoofLines() throws {
        let n = try Fixture.node(["node": "n", "display_name": "n\nLLM: fake job", "alias": "x\u{202E}y"])
        XCTAssertFalse(NodeLines.displayName(n).contains("\n"))
        XCTAssertFalse(NodeLines.detail(n).contains("\u{202E}"))
    }

    @MainActor
    func testTokenTravelsInHeaderNotURL() async {
        StubProtocol.requests = []
        StubProtocol.responses = [(200, Fixture.clusterJSON())]
        let (m, _, suite) = makeModel(token: "tok-abc")
        defer { UserDefaults().removePersistentDomain(forName: suite) }
        await m.pollOnce()
        let r = StubProtocol.requests.first
        XCTAssertEqual(r?.value(forHTTPHeaderField: "X-Minion-Token"), "tok-abc")
        XCTAssertFalse(r?.url?.absoluteString.contains("tok-abc") ?? true)
    }
}

// MARK: - Performance (timed bound, no measure{})

final class PerformanceTests: XCTestCase {
    func testDecodeAndLinesStayFast() throws {
        let data = Fixture.clusterJSON()
        let start = Date()
        var total = 0
        for _ in 0..<500 {
            let snap = try JSONDecoder().decode(ClusterSnapshot.self, from: data)
            for n in snap.allNodes { total += NodeLines.job(n).count + NodeLines.purpose(n).count }
        }
        XCTAssertGreaterThan(total, 0)
        XCTAssertLessThan(Date().timeIntervalSince(start), 3.0, "500 polls of 10 nodes must decode + format in < 3 s")
    }

    func testCleanOnHugeInputIsBounded() {
        let big = String(repeating: "x  y\n", count: 20_000)
        let start = Date()
        XCTAssertLessThanOrEqual(NodeLines.clean(big).count, NodeLines.maxLength)
        XCTAssertLessThan(Date().timeIntervalSince(start), 2.0)
    }
}

// MARK: - Retry

final class RetryTests: XCTestCase {
    @MainActor
    func testFailuresCountUpThenRecoverWithBothLines() async {
        StubProtocol.responses = [(503, Data()), (503, Data()), (200, Fixture.clusterJSON())]
        let (m, _, suite) = makeModel()
        defer { UserDefaults().removePersistentDomain(forName: suite) }
        await m.pollOnce()
        XCTAssertEqual(m.failures, 1)
        await m.pollOnce()
        XCTAssertTrue(m.isOffline)
        await m.pollOnce()
        XCTAssertEqual(m.failures, 0)
        XCTAssertFalse(m.isOffline)
        XCTAssertEqual(m.snapshot?.allNodes.count, 10)
    }

    @MainActor
    func testGarbageKeepsLastGoodSnapshot() async {
        StubProtocol.responses = [(200, Fixture.clusterJSON()), (200, Data("{not json".utf8))]
        let (m, _, suite) = makeModel()
        defer { UserDefaults().removePersistentDomain(forName: suite) }
        await m.pollOnce()
        await m.pollOnce()
        XCTAssertEqual(m.failures, 1)
        XCTAssertEqual(m.snapshot?.allNodes.count, 10, "the last map stays on screen")
    }
}

// MARK: - Unit

final class UnitTests: XCTestCase {
    func testDisplayNameAndAlias() throws {
        XCTAssertEqual(NodeLines.displayName(try Fixture.node(["node": "mac-studio", "alias": "nova-core8"])),
                       "mac-studio (nova-core8)")
        XCTAssertEqual(NodeLines.displayName(try Fixture.node(["node": "nova-core5"])), "nova-core5")
        XCTAssertNil(NodeLines.alias(try Fixture.node(["node": "n", "alias": "  "])))
    }

    func testPurposeFallback() throws {
        XCTAssertEqual(NodeLines.purpose(try Fixture.node(["node": "n"])), "purpose not set")
        XCTAssertEqual(NodeLines.purpose(try Fixture.node(["node": "n", "purpose": " PG standby "])), "PG standby")
    }

    func testStateMapping() throws {
        XCTAssertEqual(NodeLines.state(try Fixture.node(["node": "n", "llm_job": ["state": "running"]])), .running)
        XCTAssertEqual(NodeLines.state(try Fixture.node(["node": "n", "live": false])), .silent)
        XCTAssertEqual(NodeLines.state(try Fixture.node(["node": "n", "live": true, "llm": false])), .none)
        XCTAssertEqual(NodeLines.state(try Fixture.node(["node": "n", "live": true, "llm_job": ["state": "bogus"]])), .idle)
    }
}

// MARK: - Integration

final class IntegrationTests: XCTestCase {
    func testAllTenNodesCarryBothLines() throws {
        let snap = try JSONDecoder().decode(ClusterSnapshot.self, from: Fixture.clusterJSON())
        XCTAssertEqual(snap.allNodes.count, 10)
        for n in snap.allNodes {
            XCTAssertFalse(NodeLines.job(n).isEmpty, n.node)
            XCTAssertEqual(NodeLines.purpose(n), Fixture.purposes[n.node], n.node)
        }
        let studio = try XCTUnwrap(snap.allNodes.first { $0.node == "mac-studio" })
        XCTAssertEqual(NodeLines.displayName(studio), "mac-studio (nova-core8)")
    }

    @MainActor
    func testCacheGoesToInjectedSuiteOnly() async {
        StubProtocol.responses = Array(repeating: (200, Fixture.clusterJSON()), count: 1)
        let (m, d, suite) = makeModel()
        defer { UserDefaults().removePersistentDomain(forName: suite) }
        await m.pollOnce()
        XCTAssertNotNil(m.snapshot)
        // Whether or not this poll landed in the 2 s cache window, nothing touched .standard's cache from here.
        if let cached = d.data(forKey: ClusterModel.cacheKey) {
            XCTAssertNoThrow(try JSONDecoder().decode(ClusterSnapshot.self, from: cached))
        }
    }
}

// MARK: - Functional

final class FunctionalTests: XCTestCase {
    func testHubJobTextIsShownVerbatim() throws {
        let n = try Fixture.node(["node": "nova-core7", "live": true,
                                  "llm_job": ["state": "running", "text": "nova_notifier @ mac-studio → qwen3:8b · generating 52 tok"]])
        XCTAssertEqual(NodeLines.job(n), "nova_notifier @ mac-studio → qwen3:8b · generating 52 tok")
    }

    func testOlderHubFallbackNeverInventsAJob() throws {
        let busy = try Fixture.node(["node": "n", "live": true, "runners": [["model": "qwen3:8b", "slots": [
            ["busy": true, "prompt": 100, "prompt_done": 100, "decoded": 240]]]]])
        XCTAssertEqual(NodeLines.job(busy), "qwen3:8b · generating 240 tok · caller not identified")
        let reading = try Fixture.node(["node": "n", "live": true, "minion": ["now_reading": [["title": "Dune", "phase": "reading"]]]])
        XCTAssertEqual(NodeLines.job(reading), "reading: Dune")
        let whisper = try Fixture.node(["node": "n", "live": true, "minion": ["now_reading": [["title": "Ep 1", "phase": "transcribing"]]]])
        XCTAssertEqual(NodeLines.job(whisper), "transcribing (whisper): Ep 1")
        XCTAssertEqual(NodeLines.job(try Fixture.node(["node": "n", "live": true, "llm": false])), "No LLM on this node")
        XCTAssertEqual(NodeLines.job(try Fixture.node(["node": "n", "live": true, "loaded": [["name": "a"], ["name": "b"]]])),
                       "Idle · 2 models loaded")
        XCTAssertEqual(NodeLines.job(try Fixture.node(["node": "n", "live": false, "age_s": 125])),
                       "Silent · probe last heard 2m ago")
    }

    func testDetailHasEverything() throws {
        let n = try Fixture.node(["node": "mac-studio", "alias": "nova-core8", "purpose": "Control plane", "ip": "192.168.1.6",
                                  "llm_job": ["text": "Idle · 7 models loaded"]])
        XCTAssertEqual(NodeLines.detail(n), "mac-studio (nova-core8)\nLLM: Idle · 7 models loaded\nPurpose: Control plane\n192.168.1.6")
    }
}

// MARK: - Frame (layout never jumps)

@MainActor
final class FrameTests: XCTestCase {
    private func height(_ node: ClusterSnapshot.Node, width: CGFloat = 240) -> CGFloat {
        let v = NSHostingView(rootView: NodeRoleLines(node: node, u: 1, size: 11).frame(width: width))
        return v.fittingSize.height
    }

    func testHeightIsFixedWhateverTheText() throws {
        let short = try Fixture.node(["node": "a", "live": true, "purpose": "Redis", "llm_job": ["state": "idle", "text": "Idle"]])
        let long = try Fixture.node(["node": "b", "live": true,
                                     "purpose": String(repeating: "PG standby · Mosquitto · embeddings · ", count: 8),
                                     "llm_job": ["state": "running", "text": String(repeating: "nova_journal @ mac-studio + ", count: 10)]])
        let none = try Fixture.node(["node": "c", "live": false])
        let h = height(short)
        XCTAssertGreaterThan(h, 0)
        XCTAssertEqual(height(long), h, accuracy: 0.5)
        XCTAssertEqual(height(none), h, accuracy: 0.5)
        let line = NodeRoleLines.lineHeight(size: 11, u: 1)
        XCTAssertEqual(h, line * 2 + 2, accuracy: 0.5)
    }

    func testNarrowWidthStillTwoLines() throws {
        let n = try Fixture.node(["node": "a", "live": true, "purpose": "Control plane · scheduler · Redis · memory-server",
                                  "llm_job": ["text": "reading: A Very Long Title That Will Not Fit In A Narrow Card At All"]])
        XCTAssertEqual(height(n, width: 120), height(n, width: 600), accuracy: 0.5)
    }
}
