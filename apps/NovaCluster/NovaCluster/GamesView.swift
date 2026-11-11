//
//  GamesView.swift
//  Nova Cluster (iPad + iPhone + Mac + Apple TV)
//
//  1.3.0: the Games screen. Everything the Nova Games platform is doing, read-only, from the hub's /games.
//  1.4.0 (2026-11-11, Jordan: "pretty-up the games section of Nova Cluster. More stats about live game play"):
//  a wall-display layout. Header tiles (playing now, play time today, sessions today, events/hr, 7-day win rate,
//  game AI); "live tables" — a card per live session with a ticking timer, APM and the last event, and for GTNW a
//  DEFCON 5→1 gauge, an approval sparkline, the game date and turn; a 60-minute activity chart stacked by game; a
//  live feed with per-kind icons and colours; today per game; the last 7 days; the hall of fame; recent sessions;
//  and the AI / relay / DB counters in one panel. Wide (Mac, iPad landscape, Apple TV) is three columns sized to fit
//  a 1080p TV without scrolling; narrow (iPad portrait, iPhone) is one scrolling column. Nothing here takes focus,
//  so the Apple TV's only focus stop stays the wordmark.
//

import Charts
import SwiftUI

// MARK: - Layout (pure, tested)

struct GamesLayout: Equatable {
    var u: CGFloat
    var wide: Bool
    var phone: Bool
    var tileColumns: Int
    var todayColumns: Int
    var feedRows: Int

    /// Mac: the window's own unit (1480×900 is 1.0). Elsewhere DeviceLayout's unit (an iPhone is a fixed 0.62).
    /// Wide = landscape with at least 1100 layout units of width; otherwise one column.
    static func make(size: CGSize, phone: Bool = DeviceLayout.isPhone, mac: Bool = GamesLayout.isMac) -> GamesLayout {
        let u: CGFloat = mac ? max(0.8, min(size.width / 1480, size.height / 900))
                             : (phone ? DeviceLayout.phoneUnit : max(0.8, min(size.width, size.height) / 820))
        let wide = size.width > size.height && size.width / u >= 1100
        return GamesLayout(u: u, wide: wide, phone: phone, tileColumns: wide ? 6 : 3,
                           todayColumns: wide ? 3 : (phone ? 2 : 3), feedRows: wide ? 15 : (phone ? 8 : 10))
    }

    /// The app-wide alert strip and "just published" ticker float over the top centre of every view; the board
    /// starts below whichever of them is showing (points).
    static func stripInset(alert: Bool, published: Bool, u: CGFloat) -> CGFloat {
        guard alert || published else { return 0 }
        return ((alert ? 26 : 0) + (published ? 22 : 0) + 4) * u
    }

    static let isMac: Bool = {
        #if os(macOS)
        return true
        #else
        return false
        #endif
    }()
}

// MARK: - Screen

struct GamesScreen<Mark: View>: View {
    let model: GamesModel
    let paused: Bool
    /// Whether the alert strip / published ticker are showing (they overlay the top of the screen).
    var alertStrip = false
    var publishedStrip = false
    @ViewBuilder let mark: (CGFloat) -> Mark

    var body: some View {
        GeometryReader { geo in
            let layout = GamesLayout.make(size: geo.size)
            ScrollView(.vertical, showsIndicators: false) {
                GamesBoard(snapshot: model.snapshot, error: model.lastError, layout: layout,
                           width: geo.size.width, paused: paused,
                           topInset: GamesLayout.stripInset(alert: alertStrip, published: publishedStrip, u: layout.u), mark: mark)
            }
        }
        .onAppear { model.start() }
        .onDisappear { model.stop() }
    }
}

/// The whole screen for one snapshot, without the scroll view or the poller (so tests can render it).
struct GamesBoard<Mark: View>: View {
    let snapshot: GamesSnapshot?
    var error: String?
    let layout: GamesLayout
    let width: CGFloat
    var paused = false
    var topInset: CGFloat = 0
    var now: Date?
    @ViewBuilder let mark: (CGFloat) -> Mark

    private var s: GamesSnapshot? { snapshot }
    private var u: CGFloat { layout.u }
    private var gap: CGFloat { 14 * u }

    var body: some View {
        let inner = max(200, width - 52 * u)
        VStack(alignment: .leading, spacing: gap) {
            header
            if let error, s == nil {
                Text(error.uppercased()).font(.system(size: 11 * u, weight: .medium)).tracking(1.5 * u)
                    .foregroundStyle(Palette.alert)
            }
            if s?.db?.up == false {
                Label("GAMES DB DOWN · \(NodeLines.clean(s?.db?.error, max: 80))", systemImage: "exclamationmark.triangle.fill")
                    .font(.system(size: 11 * u, weight: .medium)).tracking(1 * u).foregroundStyle(Palette.alert)
            }
            if layout.wide {
                let left = inner * 0.40, mid = inner * 0.30, right = inner - left - mid - 2 * gap
                HStack(alignment: .top, spacing: gap) {
                    VStack(alignment: .leading, spacing: gap) {
                        liveTables(width: left)
                        activity(width: left)
                        todayPanel(width: left)
                    }
                    .frame(width: left)
                    VStack(alignment: .leading, spacing: gap) { feedPanel }
                        .frame(width: mid)
                    VStack(alignment: .leading, spacing: gap) {
                        weekPanel
                        famePanel
                        recentPanel(rows: 5)
                        systemPanel
                    }
                    .frame(width: max(100, right))
                }
            } else {
                liveTables(width: inner)
                activity(width: inner)
                feedPanel
                todayPanel(width: inner)
                weekPanel
                famePanel
                recentPanel(rows: 6)
                systemPanel
            }
        }
        .padding(.horizontal, 26 * u)
        .padding(.top, 14 * u + topInset)
        .padding(.bottom, 24 * u)
        .frame(width: width, alignment: .topLeading)
    }

    // MARK: Header

    private var header: some View {
        let t = GamesStats.totals(s)
        let tiles: [AnyView] = [
            AnyView(GamesTile(value: "\(t.playing)", unit: t.playing == 1 ? "session" : "sessions", label: "PLAYING NOW", u: u,
                              tint: t.playing > 0 ? Palette.warm : nil, live: t.playing > 0)),
            AnyView(GamesTile(value: t.minutesToday.map { GamesStats.minutes($0) } ?? "—", unit: nil, label: "PLAY TIME TODAY", u: u)),
            AnyView(GamesTile(value: "\(t.sessionsToday)", unit: nil, label: "SESSIONS TODAY", u: u)),
            AnyView(GamesTile(value: Fmt.int(t.eventsHour), unit: "/ hr", label: "PLAY EVENTS", u: u,
                              tint: t.eventsHour > 0 ? Palette.cool : nil)),
            AnyView(GamesTile(value: GamesStats.pct(t.winRate), unit: s?.week.map { "\($0.wins ?? 0)W \($0.losses ?? 0)L" },
                              label: "WIN RATE · 7 DAYS", u: u,
                              tint: (t.winRate ?? 0) >= 0.5 ? Palette.gold.color() : nil)),
            AnyView(aiTile),
        ]
        return HStack(alignment: .top, spacing: 12 * u) {
            if layout.wide {
                HStack(alignment: .top, spacing: 26 * u) { ForEach(tiles.indices, id: \.self) { tiles[$0] } }
            } else {
                EvenGrid(count: tiles.count, columns: layout.tileColumns, spacing: 12 * u) { tiles[$0] }
            }
            Spacer(minLength: 0)
            mark(u)
        }
    }

    private var aiTile: GamesTile {
        let share = s?.ai_share
        if let share, (share.ai ?? 0) + (share.other ?? 0) > 0 {
            return GamesTile(value: GamesStats.pct(share.pct), unit: "AI-written", label: "GAME AI TODAY", u: u,
                             tint: (share.ai ?? 0) > 0 ? Palette.violet.color() : nil)
        }
        let tok = s?.llm?.tok_s ?? 0
        return GamesTile(value: Fmt.one(tok), unit: "tok/s", label: "GAME AI NOW", u: u, tint: tok > 0 ? Palette.cool : nil)
    }

    // MARK: Live tables

    @ViewBuilder private func liveTables(width: CGFloat) -> some View {
        let live = s?.live ?? []
        GamesPanel(title: "LIVE TABLES", u: u, trailing: live.isEmpty ? nil : "\(live.count) at the table") {
            if live.isEmpty {
                EmptyTable(last: GamesStats.lastPlayed(s), today: GamesStats.totals(s), u: u, now: now)
            } else {
                let cols = live.count > 1 && width / u > 700 ? 2 : 1
                let shown = Array(live.prefix(layout.wide ? 2 : 6))
                EvenGrid(count: shown.count, columns: cols, spacing: 10 * u) { i in
                    LiveTableCard(s: shown[i], u: u, compact: cols > 1, paused: paused, now: now)
                }
                if layout.wide && live.count > 2 {
                    Text("+ \(live.count - 2) more at the table").font(.system(size: 11 * u, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.45))
                }
            }
        }
    }

    // MARK: Activity

    private func activity(width: CGFloat) -> some View {
        let points = GamesStats.timeline(s?.timeline)
        let games = GamesStats.games(points.map(\.game))
        let total = points.reduce(0) { $0 + $1.count }
        var perMinute: [Int: Int] = [:]
        for p in points { perMinute[p.minute, default: 0] += p.count }
        let peak = perMinute.values.max() ?? 0
        return GamesPanel(title: "ACTIVITY · LAST 60 MIN", u: u,
                          trailing: total > 0 ? "\(Fmt.int(total)) events · peak \(peak)/min" : nil) {
            if s?.timeline == nil {
                GamesEmpty(text: "The hub has no activity timeline yet.", u: u)
            } else {
                Chart(points) { p in
                    BarMark(x: .value("Minute", p.minute), y: .value("Events", p.count),
                            width: .fixed(max(2, (width - 70 * u) / 60 * 0.7)))
                        .foregroundStyle(by: .value("Game", GameNames.display(p.game)))
                        .cornerRadius(1.5 * u)
                }
                .chartForegroundStyleScale(domain: games.map { GameNames.display($0) },
                                           range: games.map { GameStyle.hue($0).color(0.9) })
                .chartLegend(.hidden)
                .chartXScale(domain: -60...0.6)
                .chartXAxis {
                    AxisMarks(values: [-60, -45, -30, -15, 0]) { v in
                        AxisGridLine().foregroundStyle(.white.opacity(0.06))
                        let m = v.as(Int.self) ?? 0
                        AxisValueLabel(anchor: m == 0 ? .topTrailing : m == -60 ? .topLeading : .top) {
                            Text(m == 0 ? "now" : "\(m)m").font(.system(size: 9 * u, design: .monospaced))
                                .foregroundStyle(.white.opacity(0.45))
                        }
                    }
                }
                .chartYAxis {
                    AxisMarks(position: .leading, values: .automatic(desiredCount: 3)) { _ in
                        AxisGridLine().foregroundStyle(.white.opacity(0.06))
                        AxisValueLabel().font(.system(size: 9 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.4))
                    }
                }
                .overlay {
                    if points.isEmpty {
                        Text("Quiet hour: no play events in the last 60 minutes.")
                            .font(.system(size: 12 * u, weight: .light, design: .serif)).italic()
                            .foregroundStyle(Palette.text.opacity(0.55))
                    }
                }
                .frame(height: (layout.wide ? 96 : 130) * u)
                if !games.isEmpty {
                    HStack(spacing: 14 * u) {
                        ForEach(games, id: \.self) { g in
                            let n = points.filter { $0.game == g }.reduce(0) { $0 + $1.count }
                            GameDot(game: g, u: u, text: "\(GameNames.display(g)) \(Fmt.int(n))")
                        }
                    }
                }
            }
        }
    }

    // MARK: Feed

    private var feedPanel: some View {
        let feed = Array((s?.feed ?? []).prefix(layout.feedRows))
        return GamesPanel(title: "LIVE FEED", u: u, trailing: feed.isEmpty ? nil : "newest first") {
            if s?.feed == nil {
                GamesEmpty(text: "The hub has no live feed yet.", u: u)
            } else if feed.isEmpty {
                GamesEmpty(text: "No play events yet.", u: u)
            } else {
                VStack(alignment: .leading, spacing: 6 * u) {
                    ForEach(feed) { e in
                        FeedRow(e: e, u: u, now: now ?? Date())
                            .transition(.asymmetric(insertion: .move(edge: .top).combined(with: .opacity), removal: .opacity))
                    }
                }
                .animation(.spring(duration: 0.55), value: feed.map(\.id))
            }
        }
    }

    // MARK: Today

    private func todayPanel(width: CGFloat) -> some View {
        let today = s?.today ?? []
        return GamesPanel(title: "TODAY BY GAME", u: u) {
            if s?.today == nil {
                GamesEmpty(text: "The hub has no daily stats yet.", u: u)
            } else if today.isEmpty {
                GamesEmpty(text: "No play yet today.", u: u)
            } else {
                let shown = Array(today.prefix(6))
                EvenGrid(count: shown.count, columns: min(layout.todayColumns, max(1, shown.count)), spacing: 10 * u) { i in
                    TodayCard(t: shown[i], u: u)
                }
            }
        }
    }

    // MARK: Week

    private var weekPanel: some View {
        let bars = GamesStats.week(s?.week).filter { !$0.game.isEmpty }
        let days = (s?.week?.days ?? []).map(GamesStats.weekday)
        let games = GamesStats.games(bars.map(\.game))
        var totals: [String: (Double, Int)] = [:]
        for b in bars { let t = totals[b.label] ?? (0, 0); totals[b.label] = (t.0 + b.minutes, t.1 + b.sessions) }
        let mins = bars.reduce(0) { $0 + $1.minutes }
        return GamesPanel(title: "LAST 7 DAYS", u: u,
                          trailing: s?.week == nil ? nil : "\(s?.week?.sessions ?? 0) sessions · \(GamesStats.minutes(mins))") {
            if s?.week == nil {
                GamesEmpty(text: "The hub has no weekly stats yet.", u: u)
            } else {
                Chart {
                    ForEach(bars) { b in
                        BarMark(x: .value("Day", b.label), y: .value("Minutes", b.minutes), width: .ratio(0.6))
                            .foregroundStyle(by: .value("Game", GameNames.display(b.game)))
                            .cornerRadius(2 * u)
                    }
                    ForEach(days, id: \.self) { d in
                        if let t = totals[d], t.1 > 0 {
                            PointMark(x: .value("Day", d), y: .value("Minutes", t.0))
                                .symbolSize(0)
                                .annotation(position: .top, spacing: 2 * u) {
                                    Text("\(t.1)").font(.system(size: 9 * u, design: .monospaced))
                                        .foregroundStyle(.white.opacity(0.55))
                                }
                        }
                    }
                }
                .chartForegroundStyleScale(domain: games.map { GameNames.display($0) },
                                           range: games.map { GameStyle.hue($0).color(0.85) })
                .chartLegend(.hidden)
                .chartXScale(domain: days)
                .chartXAxis {
                    AxisMarks { _ in
                        AxisValueLabel().font(.system(size: 9 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.5))
                    }
                }
                .chartYAxis {
                    AxisMarks(position: .leading, values: .automatic(desiredCount: 3)) { v in
                        AxisGridLine().foregroundStyle(.white.opacity(0.06))
                        AxisValueLabel {
                            Text(GamesStats.minutes(v.as(Double.self))).font(.system(size: 8.5 * u, design: .monospaced))
                                .foregroundStyle(.white.opacity(0.4))
                        }
                    }
                }
                .frame(height: (layout.wide ? 92 : 120) * u)
            }
        }
    }

    // MARK: Hall of fame

    private var famePanel: some View {
        GamesPanel(title: "HALL OF FAME", u: u) {
            let fame = s?.fame ?? []
            if s?.fame == nil {
                GamesEmpty(text: "The hub has no hall of fame yet.", u: u)
            } else if fame.isEmpty {
                GamesEmpty(text: "No scores yet.", u: u)
            } else {
                VStack(alignment: .leading, spacing: 5 * u) {
                    ForEach(Array(fame.enumerated()), id: \.offset) { i, f in FameRow(rank: i + 1, f: f, u: u) }
                }
            }
        }
    }

    // MARK: Recent

    private func recentPanel(rows: Int) -> some View {
        GamesPanel(title: "RECENT SESSIONS", u: u) {
            let recent = Array((s?.recent ?? []).prefix(rows))
            if recent.isEmpty {
                GamesEmpty(text: "No sessions recorded.", u: u)
            } else {
                VStack(alignment: .leading, spacing: 5 * u) {
                    ForEach(recent) { p in RecentRow(p: p, u: u, now: now ?? Date()) }
                }
            }
        }
    }

    // MARK: AI · relay · DB

    private var systemPanel: some View {
        GamesPanel(title: "GAME AI · RELAY · DB", u: u) {
            VStack(alignment: .leading, spacing: 6 * u) {
                if let share = s?.ai_share, (share.ai ?? 0) + (share.other ?? 0) > 0 {
                    AIShareBar(share: share, u: u)
                }
                let today = s?.ai?.today
                kv("CASTING", (today?.calls ?? 0) > 0
                   ? "\(today?.calls ?? 0) AI calls today · \(today?.tok_s.map { Fmt.one($0) } ?? "—") tok/s"
                   : "no AI calls today")
                if let c = s?.ai?.calls?.first {
                    kv("", "\(c.ok == false ? "✕" : "✓") \(c.role ?? "") · \(ModelHue.short(c.model ?? "default")) · \(c.tokens_out ?? 0) tok · \(GameNames.ago(c.t))",
                       tint: c.ok == false ? Palette.alert : nil)
                }
                let running = s?.llm?.running ?? []
                if running.isEmpty {
                    kv("CLUSTER", (s?.llm?.recent ?? []).first.map { "idle · last \($0.node) \(GameNames.ago($0.at))" } ?? "no game on the cluster's models")
                } else {
                    ForEach(Array(running.prefix(2).enumerated()), id: \.offset) { i, r in
                        kv(i == 0 ? "CLUSTER" : "", "● \(r.node) → \(ModelHue.short(r.model ?? "?")) · \(Fmt.one(r.tok_s)) tok/s", tint: Palette.cool)
                    }
                }
                if let r = s?.relay {
                    if r.status == "ok" {
                        kv("RELAY", "ok \(r.ok ?? 0) · fallback \(r.fallback ?? 0) · in flight \(r.in_flight ?? 0) · polls failed \(r.polls_failed ?? 0)")
                    } else {
                        kv("RELAY", "down", tint: Palette.alert)
                    }
                }
                if let db = s?.db {
                    kv("DB", db.up == true ? "\(db.host ?? "up") · \(Int(db.ms ?? 0)) ms" + ((s?.partial?.isEmpty ?? true) ? "" : " · partial")
                                           : "down · \(NodeLines.clean(db.error, max: 60))",
                       tint: db.up == true ? nil : Palette.alert)
                }
            }
        }
    }

    private func kv(_ k: String, _ v: String, tint: Color? = nil) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8 * u) {
            Text(k).font(.system(size: 8.5 * u, weight: .semibold)).tracking(1.2 * u).foregroundStyle(.white.opacity(0.38))
                .frame(width: 58 * u, alignment: .leading)
            Text(v).font(.system(size: 11 * u, design: .monospaced)).foregroundStyle(tint ?? Palette.text.opacity(0.85))
                .lineLimit(1).truncationMode(.tail)
        }
    }
}

// MARK: - Pieces

/// A non-lazy grid of equal columns (renders in ImageRenderer, unlike LazyVGrid); short rows are padded.
struct EvenGrid<Cell: View>: View {
    let count: Int
    let columns: Int
    let spacing: CGFloat
    @ViewBuilder let cell: (Int) -> Cell

    var body: some View {
        let c = max(1, columns)
        Grid(alignment: .topLeading, horizontalSpacing: spacing, verticalSpacing: spacing) {
            ForEach(Array(stride(from: 0, to: count, by: c)), id: \.self) { r in
                let end = min(r + c, count)
                GridRow {
                    ForEach(r..<end, id: \.self) { i in cell(i).frame(maxWidth: .infinity, alignment: .topLeading) }
                    ForEach(0..<(c - (end - r)), id: \.self) { _ in Color.clear.frame(maxWidth: .infinity, maxHeight: 0) }
                }
            }
        }
    }
}

/// The house stat tile (StatTile's look), with an optional live pulse.
struct GamesTile: View {
    let value: String
    let unit: String?
    let label: String
    let u: CGFloat
    var tint: Color?
    var live = false

    var body: some View {
        VStack(alignment: .leading, spacing: 2 * u) {
            HStack(alignment: .firstTextBaseline, spacing: 4 * u) {
                Text(value).font(.system(size: 22 * u, weight: .light).monospacedDigit())
                    .foregroundStyle(tint ?? Palette.text).contentTransition(.numericText())
                if let unit { Text(unit).font(.system(size: 11 * u)).foregroundStyle(.white.opacity(0.45)) }
            }
            HStack(spacing: 4 * u) {
                if live { LiveDot(u: u, size: 5) }
                Text(label).font(.system(size: 8.5 * u, weight: .medium)).tracking(1.5 * u).foregroundStyle(.white.opacity(0.4))
            }
        }
        .lineLimit(1)
        .fixedSize()
    }
}

struct GamesPanel<Content: View>: View {
    let title: String
    let u: CGFloat
    var trailing: String?
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8 * u) {
            HStack(alignment: .firstTextBaseline) {
                Text(title).font(.system(size: 9.5 * u, weight: .semibold)).tracking(2 * u).foregroundStyle(Palette.warm.opacity(0.85))
                Spacer(minLength: 6 * u)
                if let trailing {
                    Text(trailing).font(.system(size: 9.5 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.4)).lineLimit(1)
                }
            }
            content()
        }
        .padding(12 * u)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12 * u, style: .continuous).fill(.white.opacity(0.035)))
        .overlay(RoundedRectangle(cornerRadius: 12 * u, style: .continuous).strokeBorder(.white.opacity(0.07), lineWidth: 1))
    }
}

struct GamesEmpty: View {
    let text: String
    let u: CGFloat
    var body: some View {
        Text(text).font(.system(size: 11.5 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.4)).lineLimit(2)
    }
}

struct LiveDot: View {
    let u: CGFloat
    var size: CGFloat = 7
    var color: Color = Palette.warm
    var body: some View {
        Image(systemName: "circle.fill").font(.system(size: size * u)).foregroundStyle(color)
            .shadow(color: color.opacity(0.8), radius: 3 * u)
            .symbolEffect(.pulse, options: .repeating)
    }
}

struct GameIcon: View {
    let game: String?
    let u: CGFloat
    var size: CGFloat = 26
    var body: some View {
        let hue = GameStyle.hue(game)
        Image(systemName: GameStyle.symbol(game))
            .font(.system(size: size * 0.5 * u, weight: .semibold))
            .foregroundStyle(hue.color())
            .frame(width: size * u, height: size * u)
            .background(Circle().fill(hue.color(0.16)))
            .overlay(Circle().strokeBorder(hue.color(0.35), lineWidth: 1))
    }
}

struct GameDot: View {
    let game: String?
    let u: CGFloat
    let text: String
    var body: some View {
        HStack(spacing: 5 * u) {
            Circle().fill(GameStyle.hue(game).color()).frame(width: 7 * u, height: 7 * u)
            Text(text).font(.system(size: 10 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.6)).lineLimit(1)
        }
    }
}

struct Chip: View {
    let text: String
    let hue: Palette.RGB
    let u: CGFloat
    var body: some View {
        Text(text).font(.system(size: 8.5 * u, weight: .bold)).tracking(1 * u)
            .foregroundStyle(hue.color())
            .padding(.horizontal, 6 * u).padding(.vertical, 2 * u)
            .background(Capsule().fill(hue.color(0.15)))
            .overlay(Capsule().strokeBorder(hue.color(0.4), lineWidth: 0.8))
            .lineLimit(1).fixedSize()
    }
}

/// One live session.
struct LiveTableCard: View {
    let s: GamesSnapshot.Session
    let u: CGFloat
    var compact = false
    var paused = false
    var now: Date?

    var body: some View {
        let hue = GameStyle.hue(s.game)
        VStack(alignment: .leading, spacing: 8 * u) {
            HStack(alignment: .center, spacing: 9 * u) {
                GameIcon(game: s.game, u: u, size: 30)
                VStack(alignment: .leading, spacing: 1 * u) {
                    HStack(spacing: 6 * u) {
                        Text(GameNames.display(s.game)).font(.system(size: 15 * u, weight: .semibold, design: .monospaced))
                            .foregroundStyle(hue.color())
                        if let b = GameStyle.badge(s.client) { Chip(text: b, hue: Palette.slate, u: u) }
                    }
                    Text(NodeLines.clean(GamesStats.who(s), max: 60))
                        .font(.system(size: 13 * u, weight: .medium)).foregroundStyle(Palette.text).lineLimit(1)
                }
                Spacer(minLength: 6 * u)
                timer
            }
            Text(subtitle).font(.system(size: 11 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.5)).lineLimit(1)
            if s.defcon != nil || !(s.polls ?? []).isEmpty {
                HStack(alignment: .center, spacing: 16 * u) {
                    if let d = s.defcon { DefconGauge(level: d, u: u) }
                    if let polls = s.polls, !polls.isEmpty { ApprovalSpark(polls: polls, u: u, compact: compact) }
                }
            }
            HStack(spacing: 16 * u) {
                stat(Fmt.int(s.actions ?? 0), "ACTIONS")
                stat(Fmt.one(GamesStats.apm(s, now: now ?? Date())), "APM")
                stat(Fmt.int(s.events ?? 0), "EVENTS")
                if !compact { stat(Fmt.int(s.events_5m ?? 0), "LAST 5 MIN") }
            }
            if let e = s.last_event {
                HStack(spacing: 7 * u) {
                    EventIcon(kind: e.kind, level: e.level, u: u, size: 18)
                    Text(NodeLines.clean(e.text, max: 140)).font(.system(size: 11.5 * u)).foregroundStyle(Palette.text.opacity(0.88))
                        .lineLimit(1).truncationMode(.tail)
                    Spacer(minLength: 4 * u)
                    Text(GameNames.ago(e.ts, now: now ?? Date())).font(.system(size: 9.5 * u, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.4)).fixedSize()
                }
            }
        }
        .padding(.horizontal, 14 * u)
        .padding(.vertical, 12 * u)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(
            RoundedRectangle(cornerRadius: 14 * u, style: .continuous)
                .fill(Color.black.opacity(0.62))
                .overlay(RoundedRectangle(cornerRadius: 14 * u, style: .continuous)
                    .fill(RadialGradient(colors: [hue.color(0.16), .clear], center: .topLeading, startRadius: 0, endRadius: 260 * u)))
                .overlay(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 2, style: .continuous).fill(hue.color(0.8)).frame(width: 3 * u).padding(.vertical, 14 * u)
                }
                .overlay(RoundedRectangle(cornerRadius: 14 * u, style: .continuous).strokeBorder(hue.color(0.22), lineWidth: 1))
        )
    }

    private var subtitle: String {
        var bits: [String] = []
        if let w = s.what, !w.isEmpty { bits.append(w) }
        if let d = s.difficulty, !d.isEmpty { bits.append(d) }
        if let gd = s.game_date, !gd.isEmpty { bits.append(gd) }
        if let t = s.turn { bits.append("turn \(t)") }
        return NodeLines.clean(bits.isEmpty ? "started \(GameNames.ago(s.started))" : bits.joined(separator: " · "), max: 120)
    }

    private var timer: some View {
        TimelineView(.periodic(from: .now, by: 1)) { tl in
            VStack(alignment: .trailing, spacing: 1 * u) {
                Text(GamesStats.clock(GamesStats.elapsed(s, now: now ?? tl.date)))
                    .font(.system(size: 22 * u, weight: .light, design: .monospaced).monospacedDigit())
                    .foregroundStyle(Palette.text)
                HStack(spacing: 4 * u) {
                    LiveDot(u: u, size: 5)
                    Text("LIVE").font(.system(size: 8.5 * u, weight: .bold)).tracking(1.5 * u).foregroundStyle(Palette.warm)
                }
            }
        }
    }

    private func stat(_ v: String, _ k: String) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(v).font(.system(size: 14 * u, weight: .regular, design: .monospaced).monospacedDigit()).foregroundStyle(Palette.text)
            Text(k).font(.system(size: 7.5 * u, weight: .medium)).tracking(1.2 * u).foregroundStyle(.white.opacity(0.38))
        }
        .lineLimit(1).fixedSize()
    }
}

/// DEFCON 5 → 1: five pips, lit from 5 down to the current level; the current one glows.
struct DefconGauge: View {
    let level: Int
    let u: CGFloat
    var body: some View {
        let lit = Defcon.lit(level)
        VStack(alignment: .leading, spacing: 3 * u) {
            HStack(spacing: 3 * u) {
                ForEach(0..<5, id: \.self) { i in
                    let l = 5 - i
                    let on = i < lit
                    Text("\(l)").font(.system(size: 10 * u, weight: .bold, design: .monospaced))
                        .foregroundStyle(on ? Color.black.opacity(0.75) : .white.opacity(0.3))
                        .frame(width: 20 * u, height: 16 * u)
                        .background(RoundedRectangle(cornerRadius: 3 * u).fill(on ? Defcon.hue(l).color(0.88) : .white.opacity(0.04)))
                        .overlay(RoundedRectangle(cornerRadius: 3 * u).strokeBorder(Defcon.hue(l).color(on ? 0.9 : 0.25), lineWidth: 1))
                        .shadow(color: l == level ? Defcon.hue(l).color(0.9) : .clear, radius: 5 * u)
                }
            }
            Text("DEFCON \(level)").font(.system(size: 7.5 * u, weight: .semibold)).tracking(1.4 * u)
                .foregroundStyle(Defcon.hue(level).color(0.9))
        }
        .accessibilityElement().accessibilityLabel("DEFCON \(level)")
    }
}

/// Approval polls as a sparkline with the latest value and its change.
struct ApprovalSpark: View {
    let polls: [Double]
    let u: CGFloat
    var compact = false
    var body: some View {
        let last = polls.last ?? 0
        let delta = polls.count > 1 ? last - polls[polls.count - 2] : 0
        let lo = (polls.min() ?? 0) - 2, hi = (polls.max() ?? 100) + 2
        HStack(alignment: .center, spacing: 8 * u) {
            if polls.count >= 2 { sparkChart(lo: lo, hi: hi) }
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .firstTextBaseline, spacing: 3 * u) {
                    Text("\(Fmt.one(last))%").font(.system(size: 14 * u, design: .monospaced).monospacedDigit()).foregroundStyle(Palette.text)
                    if abs(delta) >= 0.05 {
                        Text("\(delta > 0 ? "▲" : "▼")\(Fmt.one(abs(delta)))").font(.system(size: 9 * u, design: .monospaced))
                            .foregroundStyle(delta > 0 ? EventStyle.hue(level: "good").color() : Palette.alert)
                    }
                }
                Text("APPROVAL").font(.system(size: 7.5 * u, weight: .medium)).tracking(1.2 * u).foregroundStyle(.white.opacity(0.38))
            }
            .fixedSize()
        }
    }
    private func sparkChart(lo: Double, hi: Double) -> some View {
        Chart(Array(polls.enumerated()), id: \.offset) { i, v in
            AreaMark(x: .value("Poll", i), yStart: .value("lo", lo), yEnd: .value("Approval", v))
                .foregroundStyle(LinearGradient(colors: [Palette.cool.opacity(0.35), Palette.cool.opacity(0.0)],
                                                startPoint: .top, endPoint: .bottom))
                .interpolationMethod(.monotone)
            LineMark(x: .value("Poll", i), y: .value("Approval", v))
                .foregroundStyle(Palette.cool).lineStyle(StrokeStyle(lineWidth: 1.6 * u)).interpolationMethod(.monotone)
        }
        .chartXAxis(.hidden).chartYAxis(.hidden).chartLegend(.hidden)
        .chartYScale(domain: lo...hi)
        .frame(width: (compact ? 70 : 110) * u, height: 30 * u)
    }
}

struct EventIcon: View {
    let kind: String?
    let level: String?
    let u: CGFloat
    var size: CGFloat = 22
    var body: some View {
        let hue = EventStyle.hue(level: level, kind: kind)
        Image(systemName: EventStyle.symbol(kind: kind, level: level))
            .font(.system(size: size * 0.5 * u, weight: .semibold))
            .foregroundStyle(hue.color())
            .frame(width: size * u, height: size * u)
            .background(RoundedRectangle(cornerRadius: size * 0.28 * u, style: .continuous).fill(hue.color(0.15)))
    }
}

struct FeedRow: View {
    let e: GamesSnapshot.FeedItem
    let u: CGFloat
    let now: Date

    var body: some View {
        let hue = EventStyle.hue(level: e.level, kind: e.kind)
        let fresh = (GameNames.parse(e.ts ?? "").map { now.timeIntervalSince($0) } ?? 999) < 10
        HStack(alignment: .center, spacing: 8 * u) {
            EventIcon(kind: e.kind, level: e.level, u: u)
            VStack(alignment: .leading, spacing: 1 * u) {
                Text(NodeLines.clean(e.text, max: 140)).font(.system(size: 11.5 * u))
                    .foregroundStyle(e.level == "info" ? Palette.text.opacity(0.78) : Palette.text)
                    .lineLimit(1).truncationMode(.tail)
                HStack(spacing: 6 * u) {
                    Circle().fill(GameStyle.hue(e.game).color()).frame(width: 5 * u, height: 5 * u)
                    Text(GameNames.display(e.game)).foregroundStyle(GameStyle.hue(e.game).color(0.8))
                    if let c = e.count, c > 1 { Text("×\(c)").foregroundStyle(hue.color(0.9)) }
                    Spacer(minLength: 4 * u)
                    Text(GameNames.ago(e.ts, now: now)).foregroundStyle(.white.opacity(0.38))
                }
                .font(.system(size: 9 * u, design: .monospaced)).lineLimit(1)
            }
        }
        .padding(.horizontal, 6 * u).padding(.vertical, 3 * u)
        .background(RoundedRectangle(cornerRadius: 7 * u, style: .continuous).fill(hue.color(fresh ? 0.10 : 0)))
    }
}

struct TodayCard: View {
    let t: GamesSnapshot.TodayGame
    let u: CGFloat
    var body: some View {
        let hue = GameStyle.hue(t.game)
        HStack(alignment: .center, spacing: 10 * u) {
            VStack(alignment: .leading, spacing: 3 * u) {
                HStack(spacing: 6 * u) {
                    GameIcon(game: t.game, u: u, size: 20)
                    Text(GameNames.display(t.game)).font(.system(size: 11.5 * u, weight: .semibold, design: .monospaced))
                        .foregroundStyle(hue.color()).lineLimit(1)
                }
                Text(GamesStats.minutes(t.minutes)).font(.system(size: 20 * u, weight: .light).monospacedDigit()).foregroundStyle(Palette.text)
                Text("\(Fmt.plural(t.sessions ?? 0, "session", "sessions")) · \(Fmt.int(t.events ?? 0)) ev")
                    .font(.system(size: 9.5 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.5)).lineLimit(1)
                if let b = t.best { Text("best \(GamesStats.score(b))").font(.system(size: 9.5 * u, design: .monospaced)).foregroundStyle(Palette.gold.color(0.8)) }
            }
            Spacer(minLength: 0)
            WinRing(t: t, u: u)
        }
        .padding(10 * u)
        .background(RoundedRectangle(cornerRadius: 10 * u, style: .continuous).fill(Color.black.opacity(0.45)))
        .overlay(RoundedRectangle(cornerRadius: 10 * u, style: .continuous).strokeBorder(hue.color(0.2), lineWidth: 1))
    }
}

/// Wins vs losses today as a ring (grey when nothing was won or lost).
struct WinRing: View {
    let t: GamesSnapshot.TodayGame
    let u: CGFloat
    var body: some View {
        let share = GamesStats.winShare(t)
        ZStack {
            Circle().stroke(share == nil ? Color.white.opacity(0.1) : Palette.alert.opacity(0.75), lineWidth: 4 * u)
            if let share {
                Circle().trim(from: 0, to: share).stroke(Palette.gold.color(), style: StrokeStyle(lineWidth: 4 * u, lineCap: .round))
                    .rotationEffect(.degrees(-90))
            }
            VStack(spacing: 0) {
                Text("\(t.wins ?? 0)–\(t.losses ?? 0)").font(.system(size: 10 * u, weight: .medium, design: .monospaced)).foregroundStyle(Palette.text)
                Text("W–L").font(.system(size: 6.5 * u, weight: .medium)).tracking(1 * u).foregroundStyle(.white.opacity(0.4))
            }
        }
        .frame(width: 42 * u, height: 42 * u)
    }
}

struct FameRow: View {
    let rank: Int
    let f: GamesSnapshot.Fame
    let u: CGFloat
    var body: some View {
        let medal: Palette.RGB = rank == 1 ? Palette.gold : rank == 2 ? Palette.slate : rank == 3 ? Palette.orange : Palette.RGB(0.4, 0.42, 0.48)
        HStack(spacing: 8 * u) {
            Text("\(rank)").font(.system(size: 10 * u, weight: .bold, design: .monospaced)).foregroundStyle(Color.black.opacity(0.8))
                .frame(width: 18 * u, height: 18 * u).background(Circle().fill(medal.color(rank <= 3 ? 0.95 : 0.6)))
            Text(GamesStats.score(f.score)).font(.system(size: 14 * u, design: .monospaced).monospacedDigit())
                .foregroundStyle(rank == 1 ? Palette.gold.color() : Palette.text).frame(width: 62 * u, alignment: .trailing)
            GameIcon(game: f.game, u: u, size: 16)
            Text(NodeLines.clean(f.who, max: 40)).font(.system(size: 11 * u)).foregroundStyle(Palette.text.opacity(0.85)).lineLimit(1)
            Spacer(minLength: 4 * u)
            if let t = f.turns { Text("\(t)t").font(.system(size: 9.5 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.4)) }
            Chip(text: OutcomeStyle.label(f.outcome, ended: true), hue: OutcomeStyle.hue(f.outcome, ended: true), u: u)
            Text(day(f.at)).font(.system(size: 9.5 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.4)).fixedSize()
        }
    }
    private func day(_ iso: String?) -> String {
        guard let iso, let d = GameNames.parse(iso) else { return "" }
        return d.formatted(.dateTime.month(.abbreviated).day())
    }
}

struct RecentRow: View {
    let p: GamesSnapshot.Session
    let u: CGFloat
    let now: Date
    var body: some View {
        HStack(spacing: 8 * u) {
            GameIcon(game: p.game, u: u, size: 16)
            Text(GameNames.display(p.game)).font(.system(size: 11 * u, weight: .medium, design: .monospaced))
                .foregroundStyle(GameStyle.hue(p.game).color()).lineLimit(1).fixedSize()
            Text(NodeLines.clean(GamesStats.who(p), max: 40)).font(.system(size: 11 * u)).foregroundStyle(Palette.text.opacity(0.75)).lineLimit(1)
            Spacer(minLength: 4 * u)
            if let sc = p.score, sc > 0 { Text(GamesStats.score(sc)).font(.system(size: 10 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.55)) }
            Chip(text: OutcomeStyle.label(p, now: now), hue: OutcomeStyle.hue(p, now: now), u: u)
            Text(GameNames.ago(p.last_seen, now: now)).font(.system(size: 9.5 * u, design: .monospaced))
                .foregroundStyle(.white.opacity(0.4)).frame(width: 52 * u, alignment: .trailing)
        }
    }
}

/// Today's events that carry a `source`: AI-written vs template / fallback / scripted.
struct AIShareBar: View {
    let share: GamesSnapshot.AIShare
    let u: CGFloat
    var body: some View {
        let ai = Double(share.ai ?? 0), other = Double(share.other ?? 0)
        let total = max(1, ai + other)
        VStack(alignment: .leading, spacing: 4 * u) {
            GeometryReader { g in
                HStack(spacing: 0) {
                    Rectangle().fill(Palette.violet.color(0.85)).frame(width: g.size.width * ai / total)
                    Rectangle().fill(Palette.slate.color(0.35))
                }
                .clipShape(Capsule())
            }
            .frame(height: 6 * u)
            Text("\(GamesStats.pct(share.pct)) AI-written today · " + sources)
                .font(.system(size: 10 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.55)).lineLimit(1)
        }
    }
    private var sources: String {
        var by: [String: Int] = [:]
        for s in share.sources ?? [] { by[s.source ?? "?", default: 0] += s.n ?? 0 }
        return by.sorted { $0.value > $1.value }.map { "\($0.key) \($0.value)" }.joined(separator: " · ")
    }
}

/// Nobody playing: the last session and today's numbers, not an empty box.
struct EmptyTable: View {
    let last: GamesSnapshot.Session?
    let today: GamesStats.Totals
    let u: CGFloat
    var now: Date?
    var body: some View {
        HStack(alignment: .center, spacing: 14 * u) {
            Image(systemName: "gamecontroller").font(.system(size: 30 * u, weight: .ultraLight)).foregroundStyle(Palette.warm.opacity(0.7))
                .frame(width: 54 * u, height: 54 * u)
                .background(Circle().fill(Palette.warm.opacity(0.07)))
            VStack(alignment: .leading, spacing: 4 * u) {
                Text("Nobody's at the table").font(.system(size: 18 * u, weight: .light, design: .serif)).italic()
                    .foregroundStyle(Palette.text.opacity(0.85))
                if let l = last {
                    let result = l.ended == nil ? "left open" : OutcomeStyle.label(l.outcome, ended: true).lowercased()
                    Text("Last played \(GameNames.ago(l.last_seen, now: now ?? Date())): \(GameNames.display(l.game)) · \(NodeLines.clean(GamesStats.who(l), max: 40)) · \(result)")
                        .font(.system(size: 11 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.55)).lineLimit(1)
                }
                Text("Today: \(Fmt.plural(today.sessionsToday, "session", "sessions"))" + (today.minutesToday.map { " · \(GamesStats.minutes($0)) played" } ?? ""))
                    .font(.system(size: 11 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.4))
            }
        }
        .padding(.vertical, 6 * u)
    }
}
