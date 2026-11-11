//
//  GamesModel.swift
//  Nova Cluster (iPad + Mac + Apple TV)
//
//  1.3.0: the Games screen's data: the hub's /games payload, the poller, and display helpers.
//  1.4.0 (2026-11-11, "pretty-up the games section … more stats about live game play"): the hub's 1.4 additions
//  (live-session stats, GTNW DEFCON / approval, activity timeline, live feed, today, 7 days, hall of fame,
//  AI share). Every new field is optional, so a 1.3 hub still decodes; GamesStats derives what the screen shows.
//

import Foundation
import Observation

// MARK: - Payload (hub GET /games)

struct GamesSnapshot: Codable, Equatable {
    struct DB: Codable, Equatable { var up: Bool?; var ms: Double?; var host: String?; var error: String? }
    struct Session: Codable, Equatable, Identifiable {
        var id: String
        var game: String?; var client: String?; var player: String?
        var started: String?; var last_seen: String?; var ended: String?
        var outcome: String?; var score: Double?; var actions: Int?
        var who: String?; var what: String?
        // 1.4 (live sessions only)
        var elapsed_s: Int?; var apm: Double?; var events_5m: Int?; var events: Int?
        var last_event: FeedItem?
        var defcon: Int?; var turn: Int?; var game_date: String?; var polls: [Double]?; var difficulty: String?
    }
    struct FeedItem: Codable, Equatable, Identifiable {
        var id: String
        var ts: String?; var game: String?; var kind: String?; var text: String?; var level: String?; var count: Int?
        init(id: String, ts: String? = nil, game: String? = nil, kind: String? = nil, text: String? = nil,
             level: String? = nil, count: Int? = nil) {
            self.id = id; self.ts = ts; self.game = game; self.kind = kind; self.text = text; self.level = level; self.count = count
        }
        // last_event has no id: fall back to kind + ts.
        init(from d: Decoder) throws {
            let c = try d.container(keyedBy: CodingKeys.self)
            ts = try c.decodeIfPresent(String.self, forKey: .ts); game = try c.decodeIfPresent(String.self, forKey: .game)
            kind = try c.decodeIfPresent(String.self, forKey: .kind); text = try c.decodeIfPresent(String.self, forKey: .text)
            level = try c.decodeIfPresent(String.self, forKey: .level); count = try c.decodeIfPresent(Int.self, forKey: .count)
            id = try c.decodeIfPresent(String.self, forKey: .id) ?? "\(kind ?? "")@\(ts ?? "")"
        }
    }
    struct Series: Codable, Equatable { var game: String; var counts: [Int] }
    struct Timeline: Codable, Equatable { var minutes: Int?; var series: [Series]? }
    struct TodayGame: Codable, Equatable, Identifiable {
        var game: String; var sessions: Int?; var minutes: Double?; var wins: Int?; var losses: Int?
        var other: Int?; var open: Int?; var best: Double?; var events: Int?
        var id: String { game }
    }
    struct WeekRow: Codable, Equatable { var day: String; var game: String; var sessions: Int?; var minutes: Double? }
    struct Week: Codable, Equatable {
        var days: [String]?; var rows: [WeekRow]?; var wins: Int?; var losses: Int?; var sessions: Int?; var win_rate: Double?
    }
    struct Fame: Codable, Equatable {
        var game: String?; var who: String?; var score: Double?; var outcome: String?; var at: String?; var turns: Int?
    }
    struct Source: Codable, Equatable { var game: String?; var source: String?; var n: Int? }
    struct AIShare: Codable, Equatable { var ai: Int?; var other: Int?; var pct: Double?; var sources: [Source]? }
    struct Game: Codable, Equatable, Identifiable {
        var game: String; var sessions: Int?; var today: Int?; var last: String?
        var id: String { game }
    }
    struct Events: Codable, Equatable { var day: Int?; var hour: Int? }
    struct AICall: Codable, Equatable {
        var t: String?; var game: String?; var role: String?; var backend: String?; var model: String?
        var ok: Bool?; var ms: Double?; var tokens_out: Int?; var tok_s: Double?
    }
    struct AIToday: Codable, Equatable { var calls: Int?; var tokens_out: Int?; var tok_s: Double? }
    struct AI: Codable, Equatable { var calls: [AICall]?; var today: AIToday? }
    struct Relay: Codable, Equatable {
        var status: String?; var ok: Int?; var fallback: Int?; var rejected: Int?
        var in_flight: Int?; var polls_failed: Int?; var yielding: String?
    }
    struct Running: Codable, Equatable { var node: String; var jobs: [String]?; var model: String?; var phase: String?; var tok_s: Double? }
    struct Recent: Codable, Equatable { var node: String; var text: String; var at: String? }
    struct LLM: Codable, Equatable { var running: [Running]?; var tok_s: Double?; var recent: [Recent]? }

    var db: DB?
    var live: [Session]?
    var recent: [Session]?
    var games: [Game]?
    var events: Events?
    var ai: AI?
    var relay: Relay?
    var llm: LLM?
    var updated: String?
    // 1.4
    var timeline: Timeline?
    var feed: [FeedItem]?
    var today: [TodayGame]?
    var week: Week?
    var fame: [Fame]?
    var ai_share: AIShare?
    var partial: [String]?
}

/// Friendly names for the platform's game ids.
enum GameNames {
    static func display(_ id: String?) -> String {
        switch id ?? "" {
        case "gtnw": return "GTNW"
        case "casting": return "Casting Director"
        case "blompie", "blompie-mac": return "Blompie"
        case "cartest": return "CarTest"
        case "stuntdriver": return "Stunt Driver"
        case "mush": return "Nova MUSH"
        case let other: return other.isEmpty ? "—" : other
        }
    }

    /// "4m ago" from an ISO-8601 timestamp (hub times carry a UTC offset).
    static func ago(_ iso: String?, now: Date = Date()) -> String {
        guard let iso, let d = parse(iso) else { return "—" }
        let s = max(0, now.timeIntervalSince(d))
        if s < 90 { return "\(Int(s))s ago" }
        if s < 5400 { return "\(Int(s / 60))m ago" }
        if s < 172_800 { return "\(Int(s / 3600))h ago" }
        return "\(Int(s / 86400))d ago"
    }

    static func parse(_ iso: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: iso) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: iso)
    }
}

// MARK: - 1.4 look + derived stats (pure, tested)

/// Per-game accent + SF Symbol, the same on cards, charts and chips.
enum GameStyle {
    static let order = ["gtnw", "blompie", "casting", "cartest", "stuntdriver", "mush"]

    /// blompie-mac and blompie are one game on screen.
    static func key(_ id: String?) -> String {
        let g = (id ?? "").lowercased()
        return g.hasPrefix("blompie") ? "blompie" : g
    }
    static func hue(_ id: String?) -> Palette.RGB {
        switch key(id) {
        case "gtnw": return Palette.RGB(1.00, 0.52, 0.40)
        case "blompie": return Palette.teal
        case "casting": return Palette.violet
        case "cartest": return Palette.blue
        case "stuntdriver": return Palette.gold
        case "mush": return Palette.lime
        default: return Palette.slate
        }
    }
    static func symbol(_ id: String?) -> String {
        switch key(id) {
        case "gtnw": return "globe.americas.fill"
        case "blompie": return "book.closed.fill"
        case "casting": return "theatermasks.fill"
        case "cartest": return "car.fill"
        case "stuntdriver": return "flag.checkered"
        case "mush": return "bubble.left.and.bubble.right.fill"
        default: return "gamecontroller.fill"
        }
    }
    /// "MAC" / "WEB" badge (the client column is mac | web; anything else is not shown).
    static func badge(_ client: String?) -> String? {
        switch (client ?? "").lowercased() {
        case "mac": return "MAC"
        case "web": return "WEB"
        default: return nil
        }
    }
}

/// Feed / last-event colour + icon from the hub's level and the event kind.
enum EventStyle {
    static func hue(level: String?, kind: String? = nil) -> Palette.RGB {
        if kind == "nuclear_strike" { return Palette.RGB(1.0, 0.30, 0.30) }
        switch level ?? "" {
        case "bad": return Palette.RGB(0.95, 0.38, 0.40)
        case "warn": return Palette.amber
        case "good": return Palette.RGB(0.42, 0.95, 0.62)
        case "gold": return Palette.gold
        case "nova": return Palette.violet
        case "start": return Palette.blue
        default: return Palette.slate
        }
    }
    static func symbol(kind: String?, level: String?) -> String {
        switch kind ?? "" {
        case "nuclear_strike": return "atom"
        case "defcon_change": return "shield.lefthalf.filled"
        case "approval_swing", "congress_swing": return "chart.line.uptrend.xyaxis"
        case "record_set": return "trophy.fill"
        case "world_war", "attack_ordered": return "flame.fill"
        case "treaty_answered", "peace_brokered": return "signature"
        case "ticker": return "newspaper.fill"
        case "nova_commentary": return "sparkles"
        case "game_over": return "flag.checkered"
        case "game_start", "start": return "play.fill"
        case "crisis_decision": return "exclamationmark.bubble.fill"
        case "impeachment_track": return "building.columns.fill"
        case "president_order": return "person.badge.shield.checkmark.fill"
        case "election_result", "midterms": return "checkmark.seal.fill"
        case "turn": return "text.book.closed.fill"
        case "drag_race", "race": return "car.fill"
        case "world_claim", "claimed_land", "land_purchased", "statehood", "renamed_territory": return "map.fill"
        default:
            switch level ?? "" {
            case "bad": return "exclamationmark.triangle.fill"
            case "good": return "checkmark.circle.fill"
            case "gold": return "star.fill"
            case "warn": return "bolt.fill"
            default: return "circle.fill"
            }
        }
    }
}

/// Session outcome chip: label + hue.
enum OutcomeStyle {
    /// The hub's live rule: open (ended NULL) and seen within the last 10 minutes.
    static let liveWindow: TimeInterval = 600

    enum State: Equatable { case live, leftOpen, ended }

    /// 1.4.1: an open session that has not been seen for 10 minutes is "left open", not live (same rule as live tables).
    static func state(_ s: GamesSnapshot.Session, now: Date) -> State {
        if s.ended != nil { return .ended }
        guard let seen = s.last_seen.flatMap(GameNames.parse) else { return .leftOpen }
        return now.timeIntervalSince(seen) < liveWindow ? .live : .leftOpen
    }
    static func label(_ s: GamesSnapshot.Session, now: Date) -> String {
        switch state(s, now: now) {
        case .live: return "LIVE"
        case .leftOpen: return "LEFT OPEN"
        case .ended: return label(s.outcome, ended: true)
        }
    }
    static func hue(_ s: GamesSnapshot.Session, now: Date) -> Palette.RGB {
        switch state(s, now: now) {
        case .live: return Palette.amber
        case .leftOpen: return Palette.RGB(0.48, 0.52, 0.58)
        case .ended: return hue(s.outcome, ended: true)
        }
    }

    static func label(_ outcome: String?, ended: Bool) -> String {
        guard ended else { return "LIVE" }
        let o = (outcome ?? "ended").lowercased()
        return o == "victory" ? "WIN" : o == "defeat" ? "LOSS" : o.uppercased()
    }
    static func hue(_ outcome: String?, ended: Bool) -> Palette.RGB {
        guard ended else { return Palette.amber }
        switch (outcome ?? "").lowercased() {
        case "victory": return Palette.gold
        case "defeat": return Palette.RGB(0.95, 0.38, 0.40)
        case "completed": return Palette.teal
        default: return Palette.slate
        }
    }
}

/// DEFCON 5 → 1 pip colours (5 calm blue … 1 red).
enum Defcon {
    static func hue(_ level: Int) -> Palette.RGB {
        switch level {
        case 5: return Palette.blue
        case 4: return Palette.RGB(0.42, 0.95, 0.62)
        case 3: return Palette.gold
        case 2: return Palette.orange
        default: return Palette.RGB(1.0, 0.30, 0.30)
        }
    }
    /// Pips lit for a level: DEFCON 5 lights one, DEFCON 1 lights all five. Out-of-range → none.
    static func lit(_ level: Int?) -> Int {
        guard let l = level, (1...5).contains(l) else { return 0 }
        return 6 - l
    }
}

enum GamesStats {
    /// "4:07", "1:02:03".
    static func clock(_ seconds: Double) -> String {
        let s = Int(max(0, seconds))
        let h = s / 3600, m = (s % 3600) / 60, sec = s % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, sec) : String(format: "%d:%02d", m, sec)
    }

    /// "0m", "42m", "2h 05m".
    static func minutes(_ m: Double?) -> String {
        let v = Int((m ?? 0).rounded())
        return v < 60 ? "\(v)m" : String(format: "%dh %02dm", v / 60, v % 60)
    }

    /// Seconds a live session has run: from `started` (so the timer ticks between polls), else the hub's elapsed_s.
    static func elapsed(_ s: GamesSnapshot.Session, now: Date) -> Double {
        if let st = s.started, let d = GameNames.parse(st) { return max(0, now.timeIntervalSince(d)) }
        return Double(s.elapsed_s ?? 0)
    }

    /// Actions per minute: the hub's figure, else actions over the elapsed minutes.
    static func apm(_ s: GamesSnapshot.Session, now: Date) -> Double {
        if let a = s.apm { return a }
        return Double(s.actions ?? 0) / max(1, elapsed(s, now: now) / 60)
    }

    /// Who is playing: the leader / country, else the player id (never anything else).
    static func who(_ s: GamesSnapshot.Session) -> String {
        if let w = s.who, !w.isEmpty { return w }
        return s.player ?? ""
    }

    struct Totals: Equatable {
        var playing: Int; var minutesToday: Double?; var sessionsToday: Int; var eventsHour: Int
        var winRate: Double?; var aiPct: Double?
    }

    /// The header tiles. A 1.3 hub (no `today` / `week`) still gives sessions today from games[].today.
    static func totals(_ s: GamesSnapshot?) -> Totals {
        let today = s?.today
        return Totals(playing: s?.live?.count ?? 0,
                      minutesToday: today.map { $0.reduce(0) { $0 + ($1.minutes ?? 0) } },
                      sessionsToday: today.map { $0.reduce(0) { $0 + ($1.sessions ?? 0) } }
                          ?? (s?.games ?? []).reduce(0) { $0 + ($1.today ?? 0) },
                      eventsHour: s?.events?.hour ?? 0,
                      winRate: s?.week?.win_rate,
                      aiPct: s?.ai_share?.pct)
    }

    /// "62%" / "—".
    static func pct(_ x: Double?) -> String {
        guard let x else { return "—" }
        return "\(Int((x * 100).rounded()))%"
    }

    struct Point: Equatable, Identifiable {
        var minute: Int; var game: String; var count: Int
        var id: String { "\(game)@\(minute)" }
    }

    /// Timeline series → chart points (minute = −59 … 0, 0 is now). Zero buckets are dropped; blompie merges.
    static func timeline(_ t: GamesSnapshot.Timeline?) -> [Point] {
        guard let series = t?.series else { return [] }
        var merged: [String: [Int]] = [:]
        for s in series {
            let k = GameStyle.key(s.game)
            var c = merged[k] ?? Array(repeating: 0, count: s.counts.count)
            if c.count < s.counts.count { c += Array(repeating: 0, count: s.counts.count - c.count) }
            for (i, n) in s.counts.enumerated() { c[i] += n }
            merged[k] = c
        }
        return merged.keys.sorted(by: orderLess).flatMap { g in
            let c = merged[g]!
            return c.enumerated().compactMap { i, n in n > 0 ? Point(minute: i - (c.count - 1), game: g, count: n) : nil }
        }
    }

    static func orderLess(_ a: String, _ b: String) -> Bool {
        let ia = GameStyle.order.firstIndex(of: a) ?? 99, ib = GameStyle.order.firstIndex(of: b) ?? 99
        return ia != ib ? ia < ib : a < b
    }

    /// Games present in a list of keys, in house order (for chart colour scales and legends).
    static func games(_ keys: [String]) -> [String] { Array(Set(keys.map(GameStyle.key))).sorted(by: orderLess) }

    struct DayBar: Equatable, Identifiable {
        var day: String; var label: String; var game: String; var minutes: Double; var sessions: Int
        var id: String { "\(day)/\(game)" }
    }

    /// 7-day stacked bars: one entry per day (an empty day still has its axis label) and game.
    static func week(_ w: GamesSnapshot.Week?) -> [DayBar] {
        guard let w else { return [] }
        let days = w.days ?? Array(Set((w.rows ?? []).map(\.day))).sorted()
        var out: [DayBar] = []
        for d in days {
            let rows = (w.rows ?? []).filter { $0.day == d }
            if rows.isEmpty { out.append(DayBar(day: d, label: weekday(d), game: "", minutes: 0, sessions: 0)); continue }
            var byGame: [String: DayBar] = [:]
            for r in rows {
                let k = GameStyle.key(r.game)
                var b = byGame[k] ?? DayBar(day: d, label: weekday(d), game: k, minutes: 0, sessions: 0)
                b.minutes += r.minutes ?? 0; b.sessions += r.sessions ?? 0
                byGame[k] = b
            }
            out += byGame.keys.sorted(by: orderLess).map { byGame[$0]! }
        }
        return out
    }

    /// "2026-11-11" → "Wed" (local calendar).
    static func weekday(_ day: String) -> String {
        let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "yyyy-MM-dd"
        guard let d = f.date(from: day) else { return day }
        f.dateFormat = "EEE"
        return f.string(from: d)
    }

    /// Approval polls → 0…1 for a sparkline (flat line when they never move).
    static func spark(_ v: [Double]) -> [Double] {
        guard let lo = v.min(), let hi = v.max() else { return [] }
        return hi - lo < 0.001 ? v.map { _ in 0.5 } : v.map { ($0 - lo) / (hi - lo) }
    }

    /// Win share for the W/L ring (nil when nothing was won or lost).
    static func winShare(_ t: GamesSnapshot.TodayGame) -> Double? {
        let w = t.wins ?? 0, l = t.losses ?? 0
        return w + l == 0 ? nil : Double(w) / Double(w + l)
    }

    /// The empty state's line: the newest session, e.g. "GTNW · Thomas Jefferson · loss".
    static func lastPlayed(_ s: GamesSnapshot?) -> GamesSnapshot.Session? { s?.recent?.first }

    /// Score as shown in the hall of fame: "9,617".
    static func score(_ x: Double?) -> String { x.map { Int($0.rounded()).formatted(.number.grouping(.automatic)) } ?? "—" }
}

// MARK: - Model

@Observable
final class GamesModel {
    private(set) var snapshot: GamesSnapshot?
    private(set) var lastError: String?
    @ObservationIgnored private var task: Task<Void, Never>?
    @ObservationIgnored private let session: URLSession
    @ObservationIgnored private let config: () -> ClusterConfig

    init(session: URLSession = .shared, config: @escaping () -> ClusterConfig = { ClusterConfig.current }) {
        self.session = session
        self.config = config
    }

    /// Polls only while the Games screen is on screen (onAppear / onDisappear).
    func start() {
        guard task == nil else { return }
        task = Task { [weak self] in
            while !Task.isCancelled {
                await self?.pollOnce()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    func stop() { task?.cancel(); task = nil }

    func pollOnce() async {
        let cfg = config()
        guard let url = cfg.gamesURL else { lastError = "not configured"; return }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 5)
        if !cfg.token.isEmpty { req.setValue(cfg.token, forHTTPHeaderField: "X-Minion-Token") }
        do {
            let (data, resp) = try await session.data(for: req)
            guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else { throw URLError(.badServerResponse) }
            let snap = try JSONDecoder().decode(GamesSnapshot.self, from: data)
            if snap != snapshot { snapshot = snap }
            lastError = nil
        } catch {
            lastError = (error as? URLError)?.code == .badServerResponse ? "hub has no /games yet" : error.localizedDescription
        }
    }
}
