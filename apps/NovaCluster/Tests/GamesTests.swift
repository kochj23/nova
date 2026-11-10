//
//  GamesTests.swift
//  1.3.0 Games screen: payload decoding, URL building, names and times.
//

import XCTest

final class GamesTests: XCTestCase {
    static let sample = """
    {"db": {"up": true, "ms": 47, "host": "core3 :5433"},
     "relay": {"status": "ok", "yielding": null, "ok": 3, "fallback": 1, "rejected": 0, "in_flight": 0, "max_in_flight": 2, "polls_failed": 14},
     "live": [{"id": "a", "game": "gtnw", "client": "mac", "player": "default", "started": "2026-11-10T14:00:00-08:00",
               "last_seen": "2026-11-10T14:05:00-08:00", "ended": null, "outcome": null, "score": null, "actions": 12,
               "who": "Donald Trump", "what": "sandbox"}],
     "recent": [], "games": [{"game": "gtnw", "sessions": 35, "today": 8, "last": "2026-11-10T14:00:10-08:00"}],
     "events": {"day": 659, "hour": 134},
     "ai": {"calls": [{"t": "2026-11-10T14:26:22-08:00", "game": "casting", "role": "agent", "backend": "ollama",
                       "model": "qwen3:30b-a3b", "ok": true, "ms": 7318, "tokens_out": 552, "tok_s": 75.4}],
            "today": {"calls": 1, "tokens_out": 552, "tok_s": 75.4}},
     "llm": {"running": [{"node": "mac-studio", "jobs": ["GTNW @ mac-studio"], "model": "qwen3:8b", "phase": "generating 40 tok", "tok_s": 51.2}],
             "tok_s": 51.2, "recent": []},
     "updated": "2026-11-10T22:30:00+00:00", "something_new": [1, 2]}
    """

    func testDecodesTheHubPayloadAndIgnoresNewFields() throws {
        let s = try JSONDecoder().decode(GamesSnapshot.self, from: Data(Self.sample.utf8))
        XCTAssertEqual(s.db?.up, true)
        XCTAssertEqual(s.live?.first?.who, "Donald Trump")
        XCTAssertEqual(s.ai?.today?.tok_s, 75.4)
        XCTAssertEqual(s.llm?.running?.first?.node, "mac-studio")
        XCTAssertEqual(s.relay?.polls_failed, 14)
    }

    func testDecodesADownDatabase() throws {
        let s = try JSONDecoder().decode(GamesSnapshot.self, from: Data(#"{"db": {"up": false, "error": "timeout"}, "relay": null}"#.utf8))
        XCTAssertEqual(s.db?.up, false)
        XCTAssertNil(s.live)
    }

    func testGamesURLSharesHostNodeAndDropsNothing() {
        var c = ClusterConfig.current
        c.hub = "192.168.1.6:37590/"; c.node = "ipad"
        let url = c.gamesURL?.absoluteString ?? ""
        XCTAssertEqual(url, "http://192.168.1.6:37590/games?node=ipad")
        c.hub = ""
        XCTAssertNil(c.gamesURL)
    }

    func testNamesAndAgo() {
        XCTAssertEqual(GameNames.display("casting"), "Casting Director")
        XCTAssertEqual(GameNames.display("blompie-mac"), "Blompie")
        XCTAssertEqual(GameNames.display(nil), "—")
        let now = GameNames.parse("2026-11-10T14:10:00-08:00")!
        XCTAssertEqual(GameNames.ago("2026-11-10T14:05:00-08:00", now: now), "5m ago")
        XCTAssertEqual(GameNames.ago("2026-11-10T22:09:30.5+00:00", now: now), "29s ago")   // fractional seconds parse; floors
        XCTAssertEqual(GameNames.ago("garbage", now: now), "—")
    }

    func testFourViewsCycle() {
        XCTAssertEqual(ViewMode.allCases.count, 4)
        XCTAssertEqual(ViewMode(stored: "games"), .games)
        XCTAssertEqual(ViewMode.games.subtitle, "· GAMES")
    }
}
