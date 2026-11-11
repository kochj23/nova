//
//  Games14SevenCatTests.swift
//  Nova Cluster 1.4.0 Games screen in the house's 7 categories (Security, Performance, Retry, Unit, Integration,
//  Functional, Frame), as extensions of the category classes in NodeLinesTests.swift: the hub's 1.4 /games payload
//  (decoded old and new), GamesStats / GameStyle / EventStyle / Defcon helpers, GamesLayout, and the board itself
//  rendered at Mac, iPad, iPhone and Apple TV sizes (it must fit a 1080p TV without scrolling).
//
//  Set NOVA_SHOTS_DIR (xcodebuild: TEST_RUNNER_NOVA_SHOTS_DIR) to also write PNGs of each layout, and
//  NOVA_GAMES_JSON (TEST_RUNNER_NOVA_GAMES_JSON) to render a real /games payload instead of the fixture.
//

import AppKit
import SwiftUI
import XCTest

enum Fixture14 {
    static let sid = "fe87e78b-3fba-405d-99fa-86e7648d2c8d"

    /// A /games payload in the hub's 1.4 shape (from a real GTNW session, 2026-11-11), plus a web Blompie table.
    static func json(now: Date = Date(), live: Bool = true) -> Data {
        let iso = ISO8601DateFormatter()
        func ago(_ s: Double) -> String { iso.string(from: now.addingTimeInterval(-s)) }
        let gtnw: [String: Any] = [
            "id": sid, "game": "gtnw", "client": "mac", "player": "default", "started": ago(247), "last_seen": ago(12),
            "actions": 8, "who": "Thomas Jefferson", "what": "sandbox", "elapsed_s": 247, "apm": 1.94, "events_5m": 111,
            "events": 111, "defcon": 1, "turn": 8, "game_date": "1801-09-20", "difficulty": "Nightmare",
            "polls": [49.0, 48.4, 49.6, 48.8, 48.2, 51.1, 56.7, 54.5, 55.2],
            "last_event": ["kind": "crisis_decision", "text": "⚔️ WAR: FRANCE ATTACKS AUSTRIA: Arm Austria ($60K in weapons) — worked",
                           "level": "warn", "ts": ago(9)]]
        let blompie: [String: Any] = [
            "id": "b1", "game": "blompie", "client": "web", "player": "guest-7", "started": ago(600), "last_seen": ago(30),
            "actions": 14, "who": "", "what": "The Clockwork Library", "elapsed_s": 600, "apm": 1.4, "events_5m": 6, "events": 15,
            "last_event": ["kind": "turn", "text": "Turn 14: Ask the figure about the symbol", "level": "info", "ts": ago(30)]]
        let kinds: [(String, String, String)] = [
            ("nuclear_strike", "☢ USA nukes BLZ: 13 warheads, 104M dead", "bad"),
            ("crisis_decision", "🌪️ GREAT DUST STORM SHUTS DOWN EGYPT EYALET: Exploit the chaos — worked", "warn"),
            ("nova_commentary", "Nova: Diplomacy has left the building.", "nova"),
            ("world_claim", "PRT claims SLE", "info"),
            ("defcon_change", "DEFCON 2 → 1", "bad"),
            ("approval_swing", "Approval 51.1% → 56.7%", "good"),
            ("treaty_answered", "Trade Agreement with GBR accepted", "good"),
            ("record_set", "Record: lowest approval 1.6 (was 2.7)", "gold"),
            ("ticker", "France seizes lightly garrisoned America's Pants from the United States.", "warn"),
            ("game_start", "New game: United States · sandbox · Nightmare · Jan 20, 1801", "start"),
        ]
        var feed: [[String: Any]] = []
        for i in 0..<20 {
            let k = kinds[i % kinds.count]
            feed.append(["id": "\(sid):\(200 - i)", "ts": ago(Double(i * 7 + 2)), "game": i == 3 ? "blompie" : "gtnw",
                         "kind": k.0, "text": k.1, "level": k.2, "count": k.0 == "world_claim" ? 8 : 1])
        }
        var counts = Array(repeating: 0, count: 60)
        for i in 40..<60 { counts[i] = [16, 29, 21, 18, 10, 12, 9, 19, 24, 0, 9, 22, 16, 1, 15, 24, 69, 2, 30, 12][i - 40] }
        var bl = Array(repeating: 0, count: 60)
        for i in 50..<60 { bl[i] = i % 3 }
        let body: [String: Any] = [
            "db": ["up": true, "ms": 44, "host": "core3 :5433"],
            "relay": ["status": "ok", "ok": 3, "fallback": 1, "in_flight": 0, "polls_failed": 14],
            "live": live ? [gtnw, blompie] : [],
            "recent": [gtnw.merging(["outcome": NSNull()]) { $1 },
                       ["id": "r2", "game": "gtnw", "client": "mac", "player": "default", "started": ago(1500), "last_seen": ago(900),
                        "ended": ago(900), "outcome": "defeat", "score": 5532, "actions": 48, "who": "United States", "what": "sandbox"],
                       ["id": "r3", "game": "blompie-mac", "client": "mac", "player": "jordan", "started": ago(90_000),
                        "last_seen": ago(86_000), "ended": ago(86_000), "outcome": "completed", "actions": 5, "who": "", "what": ""]],
            "games": [["game": "gtnw", "sessions": 40, "today": 2, "last": ago(12)]],
            "events": ["day": 659, "hour": 316],
            "ai": ["calls": [["t": ago(400), "game": "casting", "role": "agent", "backend": "ollama", "model": "qwen3:30b-a3b",
                              "ok": true, "ms": 7318, "tokens_out": 552, "tok_s": 75.4]],
                   "today": ["calls": 1, "tokens_out": 552, "tok_s": 75.4]],
            "llm": ["running": [["node": "mac-studio", "jobs": ["GTNW @ mac-studio"], "model": "qwen3:8b", "phase": "generating", "tok_s": 51.2]],
                    "tok_s": 51.2, "recent": []],
            "timeline": ["minutes": 60, "series": [["game": "gtnw", "counts": counts], ["game": "blompie-mac", "counts": bl]]],
            "feed": feed,
            "today": [["game": "gtnw", "sessions": 2, "minutes": 15.0, "wins": 1, "losses": 1, "other": 0, "open": 1, "best": 5532, "events": 316],
                      ["game": "blompie", "sessions": 1, "minutes": 10.0, "wins": 0, "losses": 0, "other": 0, "open": 1, "events": 15]],
            "week": ["days": ["2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10", "2026-11-11"],
                     "rows": [["day": "2026-11-05", "game": "gtnw", "sessions": 1, "minutes": 6.3],
                              ["day": "2026-11-06", "game": "gtnw", "sessions": 2, "minutes": 25.5],
                              ["day": "2026-11-10", "game": "gtnw", "sessions": 11, "minutes": 35.6],
                              ["day": "2026-11-11", "game": "gtnw", "sessions": 2, "minutes": 15.0],
                              ["day": "2026-11-11", "game": "blompie-mac", "sessions": 1, "minutes": 10.0]],
                     "wins": 1, "losses": 13, "sessions": 16, "win_rate": 0.071],
            "fame": [["game": "gtnw", "who": "United States", "score": 9617, "outcome": "defeat", "at": "2026-10-20T19:22:33-07:00", "turns": 198],
                     ["game": "gtnw", "who": "United States", "score": 8376, "outcome": "defeat", "at": "2026-11-06T10:56:28-08:00", "turns": 46],
                     ["game": "gtnw", "who": "Bill Clinton", "score": 5532, "outcome": "victory", "at": "2026-11-10T11:07:01-08:00", "turns": 42],
                     ["game": "gtnw", "who": "United States", "score": 5078, "outcome": "defeat", "at": "2026-10-20T19:09:26-07:00", "turns": 13],
                     ["game": "gtnw", "who": "United States", "score": 3849, "outcome": "defeat", "at": "2026-10-22T16:10:17-07:00", "turns": 11]],
            "ai_share": ["ai": 6, "other": 60, "pct": 0.091,
                         "sources": [["game": "gtnw", "source": "template", "n": 58], ["game": "blompie-mac", "source": "llm", "n": 6],
                                     ["game": "blompie-mac", "source": "fallback", "n": 2]]],
            "updated": iso.string(from: now),
        ]
        return try! JSONSerialization.data(withJSONObject: body)
    }

    static func snapshot(now: Date = Date(), live: Bool = true) -> GamesSnapshot {
        try! JSONDecoder().decode(GamesSnapshot.self, from: json(now: now, live: live))
    }

    /// A real payload when NOVA_GAMES_JSON points at one (screenshots), else the fixture.
    static func renderSnapshot() -> GamesSnapshot {
        if let p = ProcessInfo.processInfo.environment["NOVA_GAMES_JSON"], let d = FileManager.default.contents(atPath: p),
           let s = try? JSONDecoder().decode(GamesSnapshot.self, from: d) { return s }
        return snapshot()
    }
}

@MainActor
enum BoardRender {
    static func view(_ s: GamesSnapshot?, size: CGSize, phone: Bool = false, mac: Bool = false, inset: Bool = false) -> some View {
        let layout = GamesLayout.make(size: size, phone: phone, mac: mac)
        return GamesBoard(snapshot: s, layout: layout, width: size.width, paused: true,
                          topInset: inset ? GamesLayout.stripInset(alert: true, published: true, u: layout.u) : 0) { u in
            VStack(alignment: .trailing, spacing: 2 * u) {
                Text("NOVA").font(.system(size: 26 * u, weight: .ultraLight)).tracking(15 * u).foregroundStyle(.white.opacity(0.85))
                Text("· GAMES").font(.system(size: 10 * u, weight: .medium)).tracking(5 * u).foregroundStyle(Palette.warm)
            }
            .frame(width: 170 * u, height: 52 * u, alignment: .topTrailing)
        }
        .environment(\.colorScheme, .dark)
    }

    /// The board's natural size at a given width.
    static func fitting(_ s: GamesSnapshot?, size: CGSize, phone: Bool = false, mac: Bool = false, inset: Bool = false) -> CGSize {
        let v = NSHostingView(rootView: view(s, size: size, phone: phone, mac: mac, inset: inset))
        return v.fittingSize
    }

    /// PNG of the board over the app's background, when NOVA_SHOTS_DIR is set.
    static func shot(_ name: String, _ s: GamesSnapshot?, size: CGSize, phone: Bool = false, mac: Bool = false, inset: Bool = false) {
        guard let dir = ProcessInfo.processInfo.environment["NOVA_SHOTS_DIR"] else { return }
        let content = ZStack(alignment: .top) {
            Palette.space
            RadialGradient(colors: [Color(red: 0.05, green: 0.06, blue: 0.12).opacity(0.6), .clear],
                           center: .init(x: 0.42, y: 0.46), startRadius: 0, endRadius: size.width * 0.6)
            view(s, size: size, phone: phone, mac: mac, inset: inset)
        }
        .frame(width: size.width, height: max(size.height, fitting(s, size: size, phone: phone, mac: mac, inset: inset).height), alignment: .top)
        let r = ImageRenderer(content: content)
        r.scale = 2
        guard let img = r.nsImage, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else { return }
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        try? png.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).png"))
    }
}

// MARK: - Security

extension SecurityTests {
    func testGames14HubTextIsCleanedAndWhoIsOnlyWhoOrPlayer() {
        var s = Fixture14.snapshot().live![0]
        s.who = ""; s.player = "player-1"
        XCTAssertEqual(GamesStats.who(s), "player-1")
        s.who = "Jefferson\u{202E}evil"
        let shown = NodeLines.clean(GamesStats.who(s), max: 60)
        XCTAssertFalse(shown.contains("\u{202E}"))
        XCTAssertNil(GameStyle.badge("192.168.1.5"), "only mac / web become a badge; nothing raw is shown")
        XCTAssertEqual(GameStyle.badge("Web"), "WEB")
    }

    func testGames14FixtureCarriesNoIPs() {
        let text = String(decoding: Fixture14.json(), as: UTF8.self)
        XCTAssertNil(text.range(of: #"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"#, options: .regularExpression))
    }
}

// MARK: - Performance

extension PerformanceTests {
    func testGames14DecodeAndDeriveStayFast() throws {
        let data = Fixture14.json()
        let t0 = Date()
        for _ in 0..<200 {
            let s = try JSONDecoder().decode(GamesSnapshot.self, from: data)
            _ = GamesStats.timeline(s.timeline); _ = GamesStats.week(s.week); _ = GamesStats.totals(s)
        }
        XCTAssertLessThan(Date().timeIntervalSince(t0), 2.0)
    }

    func testGames14TimelineIsBoundedByMinutesTimesGames() {
        let series = (0..<6).map { GamesSnapshot.Series(game: "g\($0)", counts: Array(repeating: 3, count: 60)) }
        XCTAssertEqual(GamesStats.timeline(.init(minutes: 60, series: series)).count, 360)
    }
}

// MARK: - Retry

extension RetryTests {
    func testGames14OlderHubStillDecodesAndFallsBack() throws {
        let s = try JSONDecoder().decode(GamesSnapshot.self, from: Data(GamesTests.sample.utf8))
        XCTAssertNil(s.feed); XCTAssertNil(s.timeline); XCTAssertNil(s.today); XCTAssertNil(s.week)
        XCTAssertNil(s.live?.first?.defcon)
        let t = GamesStats.totals(s)
        XCTAssertEqual(t.sessionsToday, 8, "a 1.3 hub's games[].today still fills the tile")
        XCTAssertNil(t.minutesToday)
        XCTAssertEqual(GamesStats.pct(t.winRate), "—")
    }

    func testGames14PartialPayloadAndOddValues() throws {
        let json = #"{"db": {"up": true}, "partial": ["timeline", "feed"], "live": [{"id": "x", "game": "gtnw", "defcon": 9, "polls": []}],"#
            + #" "feed": [{"id": "a", "level": "nonsense", "kind": null}], "week": {"rows": []}}"#
        let s = try JSONDecoder().decode(GamesSnapshot.self, from: Data(json.utf8))
        XCTAssertEqual(s.partial, ["timeline", "feed"])
        XCTAssertEqual(Defcon.lit(s.live?.first?.defcon), 0)
        XCTAssertEqual(GamesStats.spark([]), [])
        XCTAssertEqual(EventStyle.hue(level: "nonsense"), Palette.slate)
        XCTAssertEqual(GamesStats.week(s.week), [])
    }
}

// MARK: - Unit

extension UnitTests {
    func testGames14Formatting() {
        XCTAssertEqual(GamesStats.clock(247), "4:07")
        XCTAssertEqual(GamesStats.clock(3723), "1:02:03")
        XCTAssertEqual(GamesStats.clock(-5), "0:00")
        XCTAssertEqual(GamesStats.minutes(15.0), "15m")
        XCTAssertEqual(GamesStats.minutes(125), "2h 05m")
        XCTAssertEqual(GamesStats.minutes(nil), "0m")
        XCTAssertEqual(GamesStats.pct(0.071), "7%")
        XCTAssertEqual(GamesStats.score(9617), "9,617")
        XCTAssertEqual(GamesStats.weekday("2026-11-11"), "Wed")
    }

    func testGames14Styles() {
        XCTAssertEqual(GameStyle.key("blompie-mac"), "blompie")
        XCTAssertEqual(GameStyle.hue("blompie-mac"), GameStyle.hue("blompie"))
        let hues = GameStyle.order.map(GameStyle.hue)
        XCTAssertEqual(Set(hues.map { "\($0.r),\($0.g),\($0.b)" }).count, hues.count, "every game has its own accent")
        XCTAssertEqual(EventStyle.symbol(kind: "nuclear_strike", level: "bad"), "atom")
        XCTAssertGreaterThan(EventStyle.hue(level: "bad", kind: "nuclear_strike").r, 0.9)
        XCTAssertEqual(EventStyle.hue(level: "gold"), Palette.gold)
        XCTAssertEqual(OutcomeStyle.label("victory", ended: true), "WIN")
        XCTAssertEqual(OutcomeStyle.label(nil, ended: false), "LIVE")
        XCTAssertEqual(Defcon.lit(5), 1); XCTAssertEqual(Defcon.lit(1), 5)
        XCTAssertEqual(GamesStats.spark([40, 50, 60]), [0, 0.5, 1])
        XCTAssertEqual(GamesStats.spark([5, 5]), [0.5, 0.5])
    }

    func testGames14ElapsedTicksFromStarted() {
        let now = Date()
        let s = Fixture14.snapshot(now: now).live![0]
        XCTAssertEqual(GamesStats.elapsed(s, now: now.addingTimeInterval(10)), 257, accuracy: 1.5)
        var noStart = s; noStart.started = nil; noStart.apm = nil
        XCTAssertEqual(GamesStats.elapsed(noStart, now: now), 247)
        XCTAssertEqual(GamesStats.apm(noStart, now: now), 8 / (247.0 / 60), accuracy: 0.01)
    }
}

// MARK: - Integration

extension IntegrationTests {
    func testGames14FullPayloadDecodes() throws {
        let s = Fixture14.snapshot()
        let g = try XCTUnwrap(s.live?.first)
        XCTAssertEqual(g.defcon, 1); XCTAssertEqual(g.turn, 8); XCTAssertEqual(g.polls?.count, 9)
        XCTAssertEqual(g.last_event?.level, "warn")
        XCTAssertTrue(g.last_event?.id.hasPrefix("crisis_decision@") == true, "last_event has no id: kind@ts")
        XCTAssertEqual(Set(s.feed!.map(\.id)).count, 20)
        XCTAssertEqual(s.ai_share?.ai, 6)
        XCTAssertEqual(s.fame?.first?.score, 9617)
    }

    @MainActor
    func testGames14ModelPollsTheNewPayload() async {
        StubProtocol.responses = [(200, Fixture14.json())]
        let cfg = ClusterConfig(hub: "http://hub.invalid:37590", node: "test-view", token: "t")
        let m = GamesModel(session: StubProtocol.session(), config: { cfg })
        await m.pollOnce()
        XCTAssertEqual(m.snapshot?.feed?.count, 20)
        XCTAssertNil(m.lastError)
    }
}

// MARK: - Functional

extension FunctionalTests {
    func testGames14TilesFromTheNewPayload() {
        let t = GamesStats.totals(Fixture14.snapshot())
        XCTAssertEqual(t.playing, 2)
        XCTAssertEqual(t.minutesToday, 25)
        XCTAssertEqual(t.sessionsToday, 3)
        XCTAssertEqual(t.eventsHour, 316)
        XCTAssertEqual(GamesStats.pct(t.winRate), "7%")
        XCTAssertEqual(GamesStats.pct(t.aiPct), "9%")
    }

    func testGames14ChartsMergeBlompieAndKeepNowLast() {
        let pts = GamesStats.timeline(Fixture14.snapshot().timeline)
        XCTAssertEqual(Set(pts.map(\.game)), ["gtnw", "blompie"])
        XCTAssertEqual(pts.map(\.minute).max(), 0)
        XCTAssertGreaterThanOrEqual(pts.map(\.minute).min() ?? 0, -59)
        XCTAssertEqual(pts.first { $0.game == "gtnw" && $0.minute == -3 }?.count, 69)
        let week = GamesStats.week(Fixture14.snapshot().week)
        XCTAssertEqual(Set(week.map(\.day)).count, 7, "every day keeps its axis slot")
        XCTAssertEqual(week.filter { $0.day == "2026-11-11" }.map(\.game), ["gtnw", "blompie"])
        XCTAssertEqual(week.first { $0.day == "2026-11-07" }?.minutes, 0)
    }

    func testGames14EmptyStateKnowsTheLastSession() {
        let s = Fixture14.snapshot(live: false)
        XCTAssertEqual(GamesStats.lastPlayed(s)?.who, "Thomas Jefferson")
        XCTAssertEqual(GamesStats.totals(s).playing, 0)
        XCTAssertEqual(GamesStats.winShare(s.today![0]), 0.5)
        XCTAssertNil(GamesStats.winShare(s.today![1]))
    }
}

// MARK: - Frame

extension FrameTests {
    func testGames14LayoutPerDevice() {
        let mac = GamesLayout.make(size: CGSize(width: 1440, height: 900), phone: false, mac: true)
        XCTAssertTrue(mac.wide); XCTAssertEqual(mac.tileColumns, 6)
        let padL = GamesLayout.make(size: CGSize(width: 1180, height: 820), phone: false, mac: false)
        XCTAssertTrue(padL.wide); XCTAssertEqual(padL.u, 1.0, accuracy: 0.001)
        let padP = GamesLayout.make(size: CGSize(width: 820, height: 1180), phone: false, mac: false)
        XCTAssertFalse(padP.wide); XCTAssertEqual(padP.tileColumns, 3)
        let phone = GamesLayout.make(size: CGSize(width: 393, height: 852), phone: true, mac: false)
        XCTAssertFalse(phone.wide); XCTAssertEqual(phone.u, DeviceLayout.phoneUnit); XCTAssertEqual(phone.todayColumns, 2)
        let tv = GamesLayout.make(size: CGSize(width: 1740, height: 960), phone: false, mac: false)
        XCTAssertTrue(tv.wide); XCTAssertGreaterThan(tv.u, 1.1)
        XCTAssertEqual(GamesLayout.stripInset(alert: false, published: false, u: 1), 0)
        XCTAssertEqual(GamesLayout.stripInset(alert: true, published: true, u: 1), 52)
        XCTAssertEqual(GamesLayout.stripInset(alert: false, published: true, u: 2), 52, "scales with the layout unit")
    }

    @MainActor
    func testGames14WideBoardFitsATVAndAMacWindowWithoutScrolling() {
        let s = Fixture14.snapshot()
        for (size, mac) in [(CGSize(width: 1740, height: 960), false), (CGSize(width: 1920, height: 1080), false),
                            (CGSize(width: 1440, height: 900), true), (CGSize(width: 1180, height: 820), false)] {
            let fit = BoardRender.fitting(s, size: size, mac: mac)
            XCTAssertLessThanOrEqual(fit.height, size.height + 0.5, "\(size) needs \(fit.height) pt")
            XCTAssertLessThanOrEqual(fit.width, size.width + 0.5, "\(size) overflows to \(fit.width) pt")
        }
        // One live table (the usual case) with the alert strip and the ticker both up still fits the TV's safe area.
        var one = s; one.live = Array(s.live!.prefix(1))
        let tv = CGSize(width: 1740, height: 960)
        XCTAssertLessThanOrEqual(BoardRender.fitting(one, size: tv, inset: true).height, tv.height + 0.5)
    }

    @MainActor
    func testGames14NarrowBoardsNeverOverflowSideways() {
        for (size, phone) in [(CGSize(width: 393, height: 852), true), (CGSize(width: 820, height: 1180), false)] {
            for s in [Fixture14.snapshot(), Fixture14.snapshot(live: false), nil] {
                let fit = BoardRender.fitting(s, size: size, phone: phone)
                XCTAssertLessThanOrEqual(fit.width, size.width + 0.5, "\(size) overflows to \(fit.width) pt")
                XCTAssertGreaterThan(fit.height, 200)
            }
        }
    }

    @MainActor
    func testGames14Screenshots() {
        let s = Fixture14.renderSnapshot()
        BoardRender.shot("games-mac-1440x900", s, size: CGSize(width: 1440, height: 900), mac: true)
        BoardRender.shot("games-tv-1920x1080", s, size: CGSize(width: 1920, height: 1080))
        BoardRender.shot("games-ipad-landscape", s, size: CGSize(width: 1180, height: 820))
        BoardRender.shot("games-ipad-portrait", s, size: CGSize(width: 820, height: 1180))
        BoardRender.shot("games-iphone", s, size: CGSize(width: 393, height: 852), phone: true)
        BoardRender.shot("games-mac-fixture-busy", Fixture14.snapshot(), size: CGSize(width: 1440, height: 900), mac: true)
        BoardRender.shot("games-mac-empty", Fixture14.snapshot(live: false), size: CGSize(width: 1440, height: 900), mac: true)
        BoardRender.shot("games-mac-with-strips", s, size: CGSize(width: 1440, height: 900), mac: true, inset: true)
        BoardRender.shot("games-mac-old-hub", try? JSONDecoder().decode(GamesSnapshot.self, from: Data(GamesTests.sample.utf8)),
                         size: CGSize(width: 1440, height: 900), mac: true)
    }
}
