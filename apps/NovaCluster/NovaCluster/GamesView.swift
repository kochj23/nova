//
//  GamesView.swift
//  Nova Cluster (iPad + Mac + Apple TV)
//
//  1.3.0: the Games screen. Everything the Nova Games platform is doing: the Games DB on core3,
//  who is playing right now, recent sessions, the AI calls the games make (and their tokens/sec),
//  the cluster LLM work that is for a game, and the relay's counters. Read-only, from the hub's /games.
//

import SwiftUI

// MARK: - Screen

struct GamesScreen<Mark: View>: View {
    let model: GamesModel
    let paused: Bool
    @ViewBuilder let mark: (CGFloat) -> Mark

    private var s: GamesSnapshot? { model.snapshot }

    var body: some View {
        GeometryReader { geo in
            #if os(macOS)
            let u = max(0.8, min(geo.size.width / 1480, geo.size.height / 900))
            #else
            let u = DeviceLayout.unit(geo.size)
            #endif
            let wide = geo.size.width > geo.size.height
            ScrollView(.vertical, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 16 * u) {
                    HStack(alignment: .top) { tiles(u: u); Spacer(); mark(u) }
                    if let err = model.lastError, s == nil {
                        Text(err.uppercased()).font(.system(size: 11 * u, weight: .medium)).tracking(1.5 * u)
                            .foregroundStyle(Palette.alert)
                    }
                    if wide {
                        HStack(alignment: .top, spacing: 18 * u) {
                            VStack(alignment: .leading, spacing: 16 * u) { livePanel(u: u); recentPanel(u: u) }
                                .frame(maxWidth: .infinity, alignment: .leading)
                            VStack(alignment: .leading, spacing: 16 * u) { aiPanel(u: u); llmPanel(u: u); gamesPanel(u: u) }
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    } else {
                        livePanel(u: u); aiPanel(u: u); llmPanel(u: u); recentPanel(u: u); gamesPanel(u: u)
                    }
                }
                .padding(.horizontal, 26 * u)
                .padding(.top, 14 * u)
                .padding(.bottom, 24 * u)
            }
        }
        .onAppear { model.start() }
        .onDisappear { model.stop() }
    }

    // Top row: the numbers at a glance.
    private func tiles(u: CGFloat) -> some View {
        let db = s?.db
        let relay = s?.relay
        return HStack(alignment: .top, spacing: 26 * u) {
            StatTile(value: db?.up == true ? "UP" : (s == nil ? "…" : "DOWN"),
                     unit: db?.ms.map { "\(Int($0)) ms" }, label: "GAMES DB",
                     u: u, tint: db?.up == true ? Palette.terminal : Palette.alert)
            StatTile(value: "\(s?.live?.count ?? 0)", unit: nil, label: "PLAYING NOW", u: u,
                     tint: (s?.live?.isEmpty ?? true) ? nil : Palette.warm)
            StatTile(value: "\(s?.events?.hour ?? 0)", unit: "/ hr", label: "PLAY EVENTS", u: u)
            StatTile(value: String(format: "%.1f", s?.llm?.tok_s ?? 0), unit: "tok/s", label: "GAME AI NOW", u: u,
                     tint: (s?.llm?.tok_s ?? 0) > 0 ? Palette.cool : nil)
            StatTile(value: s?.ai?.today?.tok_s.map { String(format: "%.0f", $0) } ?? "—", unit: "tok/s",
                     label: "CASTING AI TODAY", u: u)
            StatTile(value: relay?.status == "ok" ? "\(relay?.ok ?? 0)" : "DOWN",
                     unit: relay.map { "fb \($0.fallback ?? 0) · in \($0.in_flight ?? 0)" }, label: "RELAY JOBS",
                     u: u, tint: relay?.status == "ok" ? nil : Palette.alert)
        }
    }

    private func panel<Content: View>(_ title: String, u: CGFloat, @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 7 * u) {
            Text(title).font(.system(size: 9.5 * u, weight: .semibold)).tracking(2 * u).foregroundStyle(Palette.warm.opacity(0.85))
            content()
        }
        .padding(12 * u)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10 * u).fill(.white.opacity(0.035)))
        .overlay(RoundedRectangle(cornerRadius: 10 * u).stroke(.white.opacity(0.07), lineWidth: 1))
    }

    private func line(_ text: String, u: CGFloat, dim: Bool = false, tint: Color? = nil) -> some View {
        Text(text).font(.system(size: 11.5 * u, design: .monospaced)).lineLimit(1).truncationMode(.tail)
            .foregroundStyle(tint ?? (dim ? .white.opacity(0.45) : Palette.text))
    }

    private func empty(_ text: String, u: CGFloat) -> some View { line(text, u: u, dim: true) }

    private func livePanel(u: CGFloat) -> some View {
        panel("PLAYING NOW", u: u) {
            if let live = s?.live, !live.isEmpty {
                ForEach(live) { p in
                    line("▸ \(GameNames.display(p.game)) · \(p.who?.isEmpty == false ? p.who! : p.player ?? "")"
                         + (p.what?.isEmpty == false ? " · \(p.what!)" : "")
                         + " · \(p.actions ?? 0) actions · \(GameNames.ago(p.last_seen))", u: u, tint: Palette.warm)
                }
            } else {
                empty("Nobody is playing right now.", u: u)
            }
        }
    }

    private func recentPanel(u: CGFloat) -> some View {
        panel("RECENT SESSIONS", u: u) {
            if let recent = s?.recent, !recent.isEmpty {
                ForEach(recent.prefix(10)) { p in
                    let result = p.ended == nil ? "in progress" : (p.outcome ?? "ended")
                    let score = p.score.map { " · \(Int($0))" } ?? ""
                    line("\(GameNames.display(p.game)) · \(p.who?.isEmpty == false ? p.who! : (p.what ?? "")) · \(result)\(score) · \(GameNames.ago(p.last_seen))",
                         u: u, dim: p.ended != nil)
                }
            } else {
                empty("No sessions recorded.", u: u)
            }
        }
    }

    private func aiPanel(u: CGFloat) -> some View {
        panel("GAME AI CALLS", u: u) {
            if let calls = s?.ai?.calls, !calls.isEmpty {
                ForEach(Array(calls.prefix(8).enumerated()), id: \.offset) { _, c in
                    let rate = c.tok_s.map { String(format: "%.0f tok/s", $0) } ?? "—"
                    line("\(c.ok == false ? "✕" : "✓") \(GameNames.display(c.game)) · \(c.role ?? "") · \(c.backend ?? "")/\(c.model ?? "default") · \(c.tokens_out ?? 0) tok · \(rate) · \(GameNames.ago(c.t))",
                         u: u, tint: c.ok == false ? Palette.alert : nil)
                }
            } else {
                empty("No AI calls logged yet.", u: u)
            }
        }
    }

    private func llmPanel(u: CGFloat) -> some View {
        panel("WHAT THE AI IS DOING FOR GAMES", u: u) {
            let running = s?.llm?.running ?? []
            ForEach(Array(running.enumerated()), id: \.offset) { _, r in
                line("● \(r.node) · \((r.jobs ?? []).joined(separator: " + ")) → \(r.model ?? "?") · \(r.phase ?? "") · \(String(format: "%.1f", r.tok_s ?? 0)) tok/s",
                     u: u, tint: Palette.cool)
            }
            let recent = s?.llm?.recent ?? []
            ForEach(Array(recent.prefix(6).enumerated()), id: \.offset) { _, r in
                line("  \(r.node) · \(r.text) · \(GameNames.ago(r.at))", u: u, dim: true)
            }
            if running.isEmpty && recent.isEmpty { empty("No game is using the cluster's models right now.", u: u) }
        }
    }

    private func gamesPanel(u: CGFloat) -> some View {
        panel("GAMES DB · ALL TITLES", u: u) {
            ForEach(s?.games ?? []) { g in
                line("\(GameNames.display(g.game).padding(toLength: 18, withPad: " ", startingAt: 0)) \(String(format: "%4d", g.sessions ?? 0)) sessions · \(g.today ?? 0) today · last \(GameNames.ago(g.last))",
                     u: u, dim: (g.today ?? 0) == 0)
            }
            if let db = s?.db, db.up != true { line("DB: \(db.error ?? "down")", u: u, tint: Palette.alert) }
        }
    }
}
