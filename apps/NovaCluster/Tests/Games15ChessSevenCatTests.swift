//
//  Games15ChessSevenCatTests.swift
//  Nova Cluster 1.5.0: the GTNW Chess rivalry card in the house's 7 categories (Security, Performance, Retry, Unit,
//  Integration, Functional, Frame), as extensions of the category classes in NodeLinesTests.swift. The hub's
//  `chess` block (decoded old and new), ChessStats, the gtnwchess GameStyle / EventStyle / OutcomeStyle entries, and
//  the board rendered with the card at Mac, TV, iPad and iPhone sizes (the wide board must still fit without scrolling).
//

import AppKit
import SwiftUI
import XCTest

enum Fixture15 {
    /// The hub's 1.5 chess block (shape from scripts/tests/test_nova_minion_hub_games_7cat.py's CHESS_ROWS).
    static func chess(now: Date = Date()) -> [String: Any] {
        let iso = ISO8601DateFormatter()
        func ago(_ s: Double) -> String { iso.string(from: now.addingTimeInterval(-s)) }
        func side(_ n: String, _ k: String, _ a: Any) -> [String: Any] { ["name": n, "kind": k, "accuracy": a] }
        return [
            "games": 8, "counted": 6, "record": ["wins": 4, "draws": 1, "losses": 1], "accuracy": ["you": 81.6, "them": 66.9],
            "rivals": [
                ["key": "h-2c1a0b9e8f", "name": "Challenger", "kind": "human", "games": 5, "wins": 3, "draws": 1, "losses": 1,
                 "streak": ["result": "win", "n": 2], "longest": ["wins": 2, "losses": 1],
                 "accuracy": ["you": 82.0, "them": 68.6, "n": 4], "last": ago(60)],
                ["key": "nova", "name": "Nova", "kind": "nova", "games": 1, "wins": 1, "draws": 0, "losses": 0,
                 "streak": ["result": "win", "n": 1], "longest": ["wins": 1, "losses": 0],
                 "accuracy": ["you": 77.0, "them": 55.0, "n": 1], "last": ago(10_800)],
            ],
            "recent": [
                ["id": "c0", "ended": ago(60), "mode": "LAN", "white": side("Little Mister", "human", 91.0),
                 "black": side("Challenger", "human", 74.0), "lm": "white", "lm_result": "win", "result": "1-0",
                 "termination": "checkmate", "opening": "C50 Italian Game", "moves": 29, "plies": 57, "level": NSNull()],
                ["id": "c2", "ended": ago(7_200), "mode": "LAN", "white": side("Challenger", "human", 80.0),
                 "black": side("Little Mister", "human", 60.0), "lm": "black", "lm_result": "loss", "result": "1-0",
                 "termination": "resignation", "opening": "B01 Scandinavian Defense", "moves": 41, "plies": 81, "level": NSNull()],
                ["id": "c4", "ended": ago(14_400), "mode": "vs Nova", "white": side("Challenger", "human", NSNull()),
                 "black": side("Nova", "nova", NSNull()), "lm": NSNull(), "lm_result": NSNull(), "result": "0-1",
                 "termination": "checkmate", "opening": "C20 King's Pawn Game", "moves": 22, "plies": 44, "level": "Captain"],
            ],
            "review": ["url": "https://nova.digitalnoise.net/nova/2026-11-11-the-italian-job/", "title": "The Italian Job",
                       "at": ago(300)],
        ]
    }

    static func json(now: Date = Date(), live: Bool = true, chess: [String: Any]? = nil) -> Data {
        var body = try! JSONSerialization.jsonObject(with: Fixture14.json(now: now, live: live)) as! [String: Any]
        body["chess"] = chess ?? Self.chess(now: now)
        return try! JSONSerialization.data(withJSONObject: body)
    }

    static func snapshot(now: Date = Date(), live: Bool = true, chess: [String: Any]? = nil) -> GamesSnapshot {
        try! JSONDecoder().decode(GamesSnapshot.self, from: json(now: now, live: live, chess: chess))
    }

    static let empty: [String: Any] = ["games": 0, "counted": 0, "record": ["wins": 0, "draws": 0, "losses": 0],
                                       "accuracy": ["you": NSNull(), "them": NSNull()], "rivals": [], "recent": [],
                                       "review": NSNull()]
}

// MARK: - Security

extension SecurityTests {
    func testChess15ReviewOpensOnlyNovasSite() {
        XCTAssertNotNil(ChessStats.reviewLink(.init(url: "https://nova.digitalnoise.net/nova/x/")))
        for bad in ["http://nova.digitalnoise.net/x", "https://nova.digitalnoise.net.evil.com/x", "javascript:alert(1)",
                    "https://evil.example/", "", "file:///etc/passwd"] {
            XCTAssertNil(ChessStats.reviewLink(.init(url: bad)), bad)
        }
        XCTAssertNil(ChessStats.reviewLink(nil))
    }

    func testChess15NamesComeOnlyFromTheHubAndAreCleaned() {
        var r = Fixture15.snapshot().chess!.rivals![0]
        r.name = "Ace\u{202E}\nevil"
        XCTAssertFalse(ChessStats.name(r).contains("\u{202E}"))
        XCTAssertFalse(ChessStats.name(r).contains("\n"))
        r.name = nil
        XCTAssertEqual(ChessStats.name(r), "Challenger", "no name from the hub = Challenger, never anything else")
        r.kind = "nova"
        XCTAssertEqual(ChessStats.name(r), "Nova")
        let text = String(decoding: Fixture15.json(), as: UTF8.self)
        XCTAssertFalse(text.contains("Amy"), "the fixture mirrors the hub: no household names")
    }
}

// MARK: - Performance

extension PerformanceTests {
    func testChess15DecodeAndDeriveStayFast() throws {
        let data = Fixture15.json()
        let t0 = Date()
        for _ in 0..<200 {
            let s = try JSONDecoder().decode(GamesSnapshot.self, from: data)
            for r in ChessStats.rivals(s.chess) { _ = ChessStats.split(r); _ = ChessStats.score(r); _ = ChessStats.streak(r.streak) }
            for g in s.chess?.recent ?? [] { _ = ChessStats.detail(g); _ = ChessStats.opponentLine(g) }
        }
        XCTAssertLessThan(Date().timeIntervalSince(t0), 2.0)
    }

    func testChess15CardIsBoundedWhateverTheHubSends() {
        var c = Fixture15.chess()
        c["rivals"] = Array(repeating: (c["rivals"] as! [[String: Any]])[0], count: 50).enumerated().map { i, r in
            var r = r; r["key"] = "h-\(i)"; return r
        }
        let s = Fixture15.snapshot(chess: c)
        XCTAssertEqual(ChessStats.rivals(s.chess).count, 3)
        XCTAssertEqual(ChessStats.rivals(s.chess, limit: 2).count, 2)
    }
}

// MARK: - Retry

extension RetryTests {
    func testChess15OlderHubHasNoCard() throws {
        let s = Fixture14.snapshot()
        XCTAssertNil(s.chess, "a 1.4 hub sends no chess block")
        XCTAssertFalse(ChessStats.show(s))
        XCTAssertFalse(ChessStats.show(nil))
        XCTAssertEqual(GamesLayout.make(size: CGSize(width: 1440, height: 900), phone: false, mac: true).feedRows(chess: false), 15)
    }

    func testChess15PartialAndOddChessBlocksDecode() throws {
        let json = #"{"chess": {"rivals": [{"key": "nova", "streak": {"result": "sideways", "n": -2}, "accuracy": {"you": 140}}],"#
            + #" "recent": [{"id": "x"}], "review": {"title": "no url"}}, "partial": ["chess_review"]}"#
        let s = try JSONDecoder().decode(GamesSnapshot.self, from: Data(json.utf8))
        let r = try XCTUnwrap(s.chess?.rivals?.first)
        XCTAssertEqual(ChessStats.streak(r.streak), "—")
        XCTAssertEqual(ChessStats.acc(r.accuracy?.you), "—", "out-of-range accuracy is not shown")
        XCTAssertNil(ChessStats.split(r))
        XCTAssertNil(ChessStats.score(r))
        XCTAssertEqual(ChessStats.record(r), "0–0–0")
        let g = try XCTUnwrap(s.chess?.recent?.first)
        XCTAssertEqual(ChessStats.chip(g).0, "—")
        XCTAssertEqual(ChessStats.detail(g), "")
        XCTAssertNil(ChessStats.reviewLink(s.chess?.review))
        XCTAssertEqual(s.partial, ["chess_review"])
    }
}

// MARK: - Unit

extension UnitTests {
    func testChess15GameStyle() {
        XCTAssertEqual(GameNames.display("gtnwchess"), "GTNW Chess")
        XCTAssertEqual(GameStyle.symbol("gtnwchess"), "crown.fill")
        XCTAssertTrue(GameStyle.order.contains("gtnwchess"))
        XCTAssertNotEqual(GameStyle.hue("gtnwchess"), GameStyle.hue("gtnw"))
        XCTAssertNotEqual(GameStyle.hue("gtnwchess"), Palette.slate, "not the unknown-game grey")
        XCTAssertEqual(EventStyle.symbol(kind: "move", level: "info"), "checkerboard.rectangle")
        XCTAssertEqual(EventStyle.symbol(kind: "move", level: "gold"), "crown.fill")
        XCTAssertEqual(EventStyle.symbol(kind: "draw_offer", level: "info"), "equal.circle.fill")
        XCTAssertEqual(EventStyle.symbol(kind: "resign", level: "warn"), "flag.fill")
        XCTAssertEqual(EventStyle.symbol(kind: "ply", level: "info"), "checkerboard.rectangle", "live plies look like moves")
        XCTAssertEqual(EventStyle.symbol(kind: "takeback", level: "warn"), "arrow.uturn.backward.circle.fill")
        XCTAssertEqual(OutcomeStyle.label("win", ended: true), "WIN")
        XCTAssertEqual(OutcomeStyle.label("loss", ended: true), "LOSS")
        XCTAssertEqual(OutcomeStyle.hue("win", ended: true), Palette.gold)
        XCTAssertEqual(OutcomeStyle.hue("draw", ended: true), Palette.teal)
    }

    func testChess15Stats() {
        let r = Fixture15.snapshot().chess!.rivals![0]
        XCTAssertEqual(ChessStats.record(r), "3–1–1")
        XCTAssertEqual(ChessStats.score(r)!, 0.7, accuracy: 0.0001)           // (3 + 0.5) / 5
        let sp = ChessStats.split(r)!
        XCTAssertEqual(sp.win + sp.draw + sp.loss, 1, accuracy: 0.0001)
        XCTAssertEqual(ChessStats.streak(r.streak), "W2")
        XCTAssertEqual(ChessStats.streak(.init(result: "loss", n: 3)), "L3")
        XCTAssertEqual(ChessStats.streakHue(.init(result: "loss", n: 1)), Palette.RGB(0.95, 0.38, 0.40))
        XCTAssertEqual(ChessStats.acc(86.6), "87")
        XCTAssertEqual(ChessStats.acc(nil), "—")
    }
}

// MARK: - Integration

extension IntegrationTests {
    func testChess15FullPayloadDecodes() throws {
        let c = try XCTUnwrap(Fixture15.snapshot().chess)
        XCTAssertEqual(c.counted, 6)
        XCTAssertEqual(c.record, .init(wins: 4, draws: 1, losses: 1))
        XCTAssertEqual(c.rivals?.map(\.kind), ["human", "nova"])
        XCTAssertEqual(c.rivals?.first?.longest?.wins, 2)
        XCTAssertEqual(c.recent?.count, 3)
        XCTAssertNil(c.recent?[2].lm)
        XCTAssertEqual(c.review?.title, "The Italian Job")
    }

    @MainActor
    func testChess15ModelPollsTheChessBlock() async {
        StubProtocol.responses = [(200, Fixture15.json())]
        let cfg = ClusterConfig(hub: "http://hub.invalid:37590", node: "test-view", token: "t")
        let m = GamesModel(session: StubProtocol.session(), config: { cfg })
        await m.pollOnce()
        XCTAssertEqual(m.snapshot?.chess?.rivals?.count, 2)
        XCTAssertTrue(ChessStats.show(m.snapshot))
        XCTAssertNil(m.lastError)
    }
}

// MARK: - Functional

extension FunctionalTests {
    func testChess15RecentGamesReadFromLittleMistersSide() {
        let g = Fixture15.snapshot().chess!.recent!
        XCTAssertEqual(ChessStats.opponentLine(g[0]), "vs Challenger")
        XCTAssertEqual(ChessStats.chip(g[0]).0, "WIN")
        XCTAssertEqual(ChessStats.sides(g[1]).me?.name, "Little Mister", "he was black")
        XCTAssertEqual(ChessStats.sides(g[1]).me?.accuracy, 60)
        XCTAssertEqual(ChessStats.chip(g[1]).0, "LOSS")
        XCTAssertEqual(ChessStats.opponentLine(g[2]), "Challenger vs Nova", "a game he didn't play names both sides by role")
        XCTAssertEqual(ChessStats.chip(g[2]).0, "0-1")
        XCTAssertEqual(ChessStats.detail(g[0]), "C50 Italian Game · checkmate · 29 moves")
    }

    func testChess15FeedGivesRoomToTheCardOnlyWhenWide() {
        let mac = GamesLayout.make(size: CGSize(width: 1440, height: 900), phone: false, mac: true)
        XCTAssertEqual(mac.feedRows(chess: true), 8)
        let phone = GamesLayout.make(size: CGSize(width: 393, height: 852), phone: true, mac: false)
        XCTAssertEqual(phone.feedRows(chess: true), phone.feedRows, "narrow scrolls: the feed keeps its rows")
        XCTAssertTrue(ChessStats.show(Fixture15.snapshot(chess: Fixture15.empty)), "an empty rivalry still shows its card")
    }
}

// MARK: - Frame

extension FrameTests {
    @MainActor
    func testChess15WideBoardWithTheCardFitsWithoutScrolling() {
        for chess in [Fixture15.chess(), Fixture15.empty] {
            let s = Fixture15.snapshot(chess: chess)
            for (size, mac) in [(CGSize(width: 1740, height: 960), false), (CGSize(width: 1920, height: 1080), false),
                                (CGSize(width: 1440, height: 900), true), (CGSize(width: 1180, height: 820), false)] {
                let fit = BoardRender.fitting(s, size: size, mac: mac)
                XCTAssertLessThanOrEqual(fit.height, size.height + 0.5, "\(size) needs \(fit.height) pt")
                XCTAssertLessThanOrEqual(fit.width, size.width + 0.5, "\(size) overflows to \(fit.width) pt")
            }
            var one = s; one.live = Array(s.live!.prefix(1))
            let tv = CGSize(width: 1740, height: 960)
            XCTAssertLessThanOrEqual(BoardRender.fitting(one, size: tv, inset: true).height, tv.height + 0.5)
        }
    }

    @MainActor
    func testChess15NarrowBoardsNeverOverflowSideways() {
        for (size, phone) in [(CGSize(width: 393, height: 852), true), (CGSize(width: 820, height: 1180), false)] {
            for s in [Fixture15.snapshot(), Fixture15.snapshot(live: false, chess: Fixture15.empty)] {
                let fit = BoardRender.fitting(s, size: size, phone: phone)
                XCTAssertLessThanOrEqual(fit.width, size.width + 0.5, "\(size) overflows to \(fit.width) pt")
            }
        }
    }

    @MainActor
    func testChess15Screenshots() {
        BoardRender.shot("chess-mac-1440x900", Fixture15.snapshot(), size: CGSize(width: 1440, height: 900), mac: true)
        BoardRender.shot("chess-tv-1920x1080", Fixture15.snapshot(), size: CGSize(width: 1920, height: 1080))
        BoardRender.shot("chess-mac-empty-rivalry", Fixture15.snapshot(live: false, chess: Fixture15.empty),
                         size: CGSize(width: 1440, height: 900), mac: true)
        BoardRender.shot("chess-iphone", Fixture15.snapshot(), size: CGSize(width: 393, height: 852), phone: true)
    }
}
