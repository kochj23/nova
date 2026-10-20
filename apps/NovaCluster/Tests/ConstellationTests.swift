//
//  ConstellationTests.swift
//  Nova Cluster 1.1.0: the constellation folded in from Nova Minion, the shared core,
//  and the three-way view switch.
//

import XCTest

final class ConstellationCoreTests: XCTestCase {
    func testPlacementIsStableAndInBounds() {
        for i in 0..<200 {
            let p = ConstellationLayout.place(work: "Work \(i % 17)", key: "k\(i)", aspect: 1.6)
            XCTAssertEqual(p, ConstellationLayout.place(work: "Work \(i % 17)", key: "k\(i)", aspect: 1.6))
            XCTAssertTrue((0.02...0.98).contains(p.norm.x), "x \(p.norm.x)")
            XCTAssertTrue((0.06...0.87).contains(p.norm.y), "y \(p.norm.y)")
            XCTAssertTrue((0..<3).contains(p.depth))
            XCTAssertTrue((0..<1).contains(p.sizeJitter))
        }
    }

    func testSameWorkSharesDepthAndClusters() {
        let a = ConstellationLayout.place(work: "Dune", key: "a", aspect: 1.6)
        let b = ConstellationLayout.place(work: "DUNE", key: "b", aspect: 1.6)   // work is case-insensitive
        XCTAssertEqual(a.depth, b.depth)
        XCTAssertLessThan(abs(a.norm.x - b.norm.x), 0.3)
        XCTAssertLessThan(abs(a.norm.y - b.norm.y), 0.4)
    }

    /// The screensaver drew with fnv1a + SplitMix64 before the move: pin the numbers so its sky didn't shift.
    func testHashAndRandomArePinned() {
        XCTAssertEqual(StableHash.fnv1a(""), 0xcbf2_9ce4_8422_2325)
        XCTAssertEqual(StableHash.fnv1a("a"), 0xaf63_dc4c_8601_ec8c)
        var r = SeededRandom(seed: 0)
        XCTAssertEqual(r.nextUInt(), 0x6e78_9e6a_a1b9_65f4)
        let u = StableHash.unit("x", 3)
        XCTAssertTrue((0..<1).contains(u))
    }

    func testLinksChainSameWork() {
        let l = ConstellationLayout.links(works: ["A", "B", "a", "C", "A"])
        XCTAssertEqual(l.map { [$0.0, $0.1] }, [[0, 2], [2, 4]])
    }

    func testLessonTextStripsCitations() {
        XCTAssertEqual(LessonText.clean("When X did Y, Z [3][12]."), "When X did Y, Z.")
        XCTAssertEqual(LessonText.clean("A [3, 4] b  c [1–2] ."), "A b c.")
    }
}

final class ViewModeTests: XCTestCase {
    func testCycleVisitsAllThreeAndWraps() {
        XCTAssertEqual(ViewMode.map.next, .dashboard)
        XCTAssertEqual(ViewMode.dashboard.next, .constellation)
        XCTAssertEqual(ViewMode.constellation.next, .map)
    }

    func testStoredFallbackAndSubtitles() {
        XCTAssertEqual(ViewMode(stored: nil), .map)
        XCTAssertEqual(ViewMode(stored: "bogus"), .map)
        XCTAssertEqual(ViewMode(stored: "constellation"), .constellation)
        XCTAssertEqual(ViewMode.constellation.subtitle, "· MINION")
    }

    func testDeepLinkAcceptsConstellation() {
        let d = UserDefaults.standard
        let before = d.string(forKey: ViewMode.key)
        defer { d.set(before, forKey: ViewMode.key) }
        XCTAssertTrue(ClusterConfig.applyView(url: URL(string: "novacluster://view?mode=constellation")!))
        XCTAssertEqual(d.string(forKey: ViewMode.key), "constellation")
        XCTAssertFalse(ClusterConfig.applyView(url: URL(string: "novacluster://view?mode=nope")!))
    }
}

final class ConstellationFleetTests: XCTestCase {
    private func node(_ n: String, minion: ClusterSnapshot.Minion?) -> ClusterSnapshot.Node {
        ClusterSnapshot.Node(node: n, minion: minion)
    }

    func testOnlyMinionsShow() {
        var s = ClusterSnapshot()
        s.nodes = [node("nova-core", minion: nil), node("mac-studio", minion: .init(state: "dedicated"))]
        XCTAssertEqual(ConstellationFleet.minions(s).map(\.node), ["mac-studio"])
        XCTAssertEqual(ConstellationFleet.minions(nil).count, 0)
    }

    func testTonightParts() {
        XCTAssertEqual(ConstellationFleet.tonightParts(.init(works: 81, lessons: 106)), ["81 works", "106 lessons"])
        XCTAssertEqual(ConstellationFleet.tonightParts(.init(works: 0, lessons: 0, transcribed: 4, audio_h: 1.6)),
                       ["4 episodes transcribed", "1.6 h audio"])
        XCTAssertEqual(ConstellationFleet.tonightParts(.init(works: 1, lessons: 2, transcribed: 1)),
                       ["1 work · 2 lessons", "1 episode"])
    }

    func testReadingText() {
        XCTAssertEqual(ConstellationFleet.readingText(.init(title: "Ep 1", phase: "transcribing")), "transcribing: Ep 1")
        XCTAssertEqual(ConstellationFleet.readingText(.init(title: "Dune", kind: "novel", phase: "mapping")),
                       "reading: Dune (novel) · mapping…")
    }
}
