//
//  DashboardView.swift
//  Nova Cluster (iPad + Mac)
//
//  The second view, in Nova Minion's dashboard style: a card per machine across the
//  top (live requests and minion jobs typed out terminal-style), a drifting starfield
//  where every finished request is a star (each machine's traffic its own
//  constellation, in that machine's hue), the latest lesson in large serif, and the
//  cluster + fleet totals along the bottom. Tap the wordmark to switch views.
//

import SwiftUI

// MARK: - Machine hues

/// One hue per machine, in map order (hub first), shared by cards, stars and legend.
struct MachineColors: Equatable {
    private(set) var order: [String] = []
    static let hues: [Palette.RGB] = [Palette.amber, Palette.blue, Palette.teal, Palette.rose, Palette.violet,
                                      Palette.lime, Palette.gold, Palette.orange, Palette.slate]
    init(stations: [StationVM]) { order = stations.map(\.name) }
    func hue(for node: String?) -> Palette.RGB {
        guard let node, let i = order.firstIndex(of: node) else { return Palette.slate }
        return Self.hues[i % Self.hues.count]
    }
}

// MARK: - Traffic starfield

struct TrafficStar {
    let key: String
    let node: String
    let depth: Int
    let norm: CGPoint
    let size: Double
    let hue: Palette.RGB
    let twinkleRate: Double
    let twinklePhase: Double
    let born: Date
    let rank: Int
}

@MainActor
final class TrafficStarModel {
    private(set) var stars: [TrafficStar] = []
    private(set) var links: [(Int, Int)] = []
    private var firstSeen: [String: Date] = [:]
    private var lastKeys: [String] = []
    private var lastColors: MachineColors?

    func update(events: [ClusterSnapshot.Event], colors: MachineColors) {
        let keys = events.map(\.key)
        guard keys != lastKeys || colors != lastColors else { return }
        let initial = lastKeys.isEmpty
        lastKeys = keys; lastColors = colors
        let now = Date()
        var built: [TrafficStar] = []
        var seen = Set<String>()
        for (rank, e) in events.prefix(100).enumerated() {
            let key = e.key
            guard seen.insert(key).inserted else { continue }
            let node = e.node ?? "hub"
            // Constellations live in the open sky between the cards and the lesson.
            let cx = 0.08 + 0.84 * StableHash.unit(node, 1)
            let cy = 0.44 + 0.36 * StableHash.unit(node, 2)
            let ang = StableHash.unit(key, 3) * 2 * .pi
            let rad = 0.02 + 0.08 * StableHash.unit(key, 4)
            let p = CGPoint(x: min(0.98, max(0.02, cx + cos(ang) * rad * 0.62)),
                            y: min(0.97, max(0.03, cy + sin(ang) * rad)))
            if firstSeen[key] == nil {
                firstSeen[key] = initial ? now.addingTimeInterval(Double(rank) * 0.03) : now
            }
            let mag = log2(Double(max(1, e.tokens)) + 1)
            built.append(TrafficStar(key: key, node: node, depth: Int(StableHash.of(node, seed: 5) % 3), norm: p,
                                     size: 0.9 + 0.32 * mag + (rank < 6 ? 0.6 : 0),
                                     hue: colors.hue(for: e.node),
                                     twinkleRate: 0.25 + 0.9 * StableHash.unit(key, 7),
                                     twinklePhase: StableHash.unit(key, 8) * 2 * .pi,
                                     born: firstSeen[key]!, rank: rank))
        }
        var lastOfNode: [String: Int] = [:]
        var l: [(Int, Int)] = []
        for (i, s) in built.enumerated() {
            if let j = lastOfNode[s.node] { l.append((j, i)) }
            lastOfNode[s.node] = i
        }
        stars = built; links = l
        let live = Set(keys)
        firstSeen = firstSeen.filter { live.contains($0.key) }
    }
}

struct TrafficStarfield: View {
    let model: TrafficStarModel
    var paused: Bool

    private static let amps: [CGFloat] = [14, 26, 42]
    private static let periods: [Double] = [260, 190, 140]
    private static let phases: [Double] = [0.0, 2.1, 4.2]

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: paused)) { tl in
            Canvas { ctx, size in draw(&ctx, size: size, now: tl.date) }
        }
        .drawingGroup()
        .allowsHitTesting(false)
    }

    private func draw(_ ctx: inout GraphicsContext, size: CGSize, now: Date) {
        let t = now.timeIntervalSinceReferenceDate
        let unit = max(0.7, min(size.width, size.height) / 820)
        let stars = model.stars
        guard !stars.isEmpty else { return }
        let offsets: [CGSize] = (0..<3).map { d in
            let w = 2 * Double.pi / Self.periods[d], ph = Self.phases[d]
            return CGSize(width: CGFloat(sin(t * w + ph)) * Self.amps[d] * unit,
                          height: CGFloat(sin(t * w * 1.37 + ph * 0.7)) * Self.amps[d] * 0.6 * unit)
        }
        let pad: CGFloat = 50 * unit
        let points = stars.map { s -> CGPoint in
            let o = offsets[s.depth]
            return CGPoint(x: -pad + s.norm.x * (size.width + 2 * pad) + o.width,
                           y: -pad + s.norm.y * (size.height + 2 * pad) + o.height)
        }
        let fades = stars.map { s -> Double in
            let age = now.timeIntervalSince(s.born)
            return age <= 0 ? 0 : min(1, age / 2.0)
        }
        var lines = Path()
        for (a, b) in model.links where min(fades[a], fades[b]) > 0.05 {
            lines.move(to: points[a]); lines.addLine(to: points[b])
        }
        ctx.stroke(lines, with: .color(Color(red: 0.86, green: 0.88, blue: 1.0, opacity: 0.11)), lineWidth: 0.8 * unit)

        ctx.blendMode = .plusLighter
        for (i, s) in stars.enumerated() {
            let f = fades[i]
            guard f > 0 else { continue }
            let p = points[i]
            let tw = 0.78 + 0.22 * sin(t * s.twinkleRate * 2 + s.twinklePhase)
            let recency = s.rank < 10 ? 1.0 : max(0.5, 1.0 - Double(s.rank) / 180)
            // A brand-new star flares as it is born.
            let flare = 1 + 1.6 * max(0, 1 - now.timeIntervalSince(s.born) / 1.6)
            let a = f * tw * [0.62, 0.82, 1.0][s.depth] * recency
            let r = CGFloat(s.size) * unit * CGFloat([0.8, 0.95, 1.15][s.depth]) * CGFloat(flare)
            let glowR = r * 5.5
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - glowR, y: p.y - glowR, width: glowR * 2, height: glowR * 2)),
                     with: .radialGradient(Gradient(colors: [s.hue.color(0.75 * a), s.hue.color(0.18 * a), s.hue.color(0)]),
                                           center: p, startRadius: 0, endRadius: glowR))
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - r * 0.75, y: p.y - r * 0.75, width: r * 1.5, height: r * 1.5)),
                     with: .color(s.hue.mixed(toWhite: 0.3).color(0.95 * a)))
        }
        ctx.blendMode = .normal
        // Ring around the newest request.
        if let i = stars.firstIndex(where: { $0.rank == 0 }), fades[i] > 0 {
            let p = points[i]
            let pulse = 0.5 + 0.5 * sin(t * 1.6)
            let rr = (15 + 4 * pulse) * unit
            ctx.stroke(Path(ellipseIn: CGRect(x: p.x - rr, y: p.y - rr, width: rr * 2, height: rr * 2)),
                       with: .color(stars[i].hue.color(0.30 + 0.25 * pulse)), lineWidth: 1.1 * unit)
        }
    }
}

// MARK: - Screen

struct DashboardScreen<Mark: View>: View {
    let cluster: ClusterModel
    let stations: [StationVM]
    let paused: Bool
    @ViewBuilder let mark: (CGFloat) -> Mark
    @State private var stars = TrafficStarModel()

    private var snap: ClusterSnapshot? { cluster.snapshot }
    private var colors: MachineColors { MachineColors(stations: stations) }

    var body: some View {
        GeometryReader { geo in
            #if os(macOS)
            let u = max(0.8, min(geo.size.width / 1480, geo.size.height / 900))
            #else
            let u = max(0.8, min(geo.size.width, geo.size.height) / 820)
            #endif
            let landscape = geo.size.width > geo.size.height
            ZStack {
                TrafficStarfield(model: stars, paused: paused).ignoresSafeArea()
                VStack(spacing: 0) {
                    #if os(macOS)
                    VStack(alignment: .trailing, spacing: 12 * u) {
                        HStack(alignment: .top) { TokenHero(ticker: cluster.ticker, u: u, compact: true, paused: paused); Spacer(); mark(u) }
                        cards(u: u, columns: max(1, Int((geo.size.width - 52 * u + 14 * u) / (264 * u))))
                    }
                    .padding(.horizontal, 26 * u)
                    .padding(.top, 14 * u)
                    #else
                    VStack(alignment: .trailing, spacing: 12 * u) {
                        HStack(alignment: .top) { TokenHero(ticker: cluster.ticker, u: u, compact: true, paused: paused); Spacer(); mark(u) }
                        cards(u: u, columns: landscape ? 4 : 3, activityLines: 1)
                    }
                    .padding(.horizontal, 22 * u)
                    .padding(.top, 18 * u)
                    #endif
                    Spacer(minLength: 8 * u)
                    DashLesson(lessons: snap?.lessons ?? [], colors: colors, u: u,
                               width: geo.size.width * (landscape ? 0.74 : 0.88))
                        .padding(.bottom, (landscape ? 22 : 40) * u)
                    DashBottom(snap: snap, colors: colors, offline: cluster.isOffline,
                               lastSuccess: cluster.lastSuccess, u: u)
                        .padding(.horizontal, 26 * u)
                        .padding(.bottom, 16 * u)
                }
            }
        }
        .onAppear { stars.update(events: snap?.allEvents ?? [], colors: colors) }
        .onChange(of: snap) { _, s in stars.update(events: s?.allEvents ?? [], colors: colors) }
    }

    private func cards(u: CGFloat, columns: Int, activityLines: Int = 3) -> some View {
        let nodes = stations
        let n = max(1, min(nodes.count, columns))
        let rows = stride(from: 0, to: nodes.count, by: n).map { Array(nodes[$0..<min($0 + n, nodes.count)]) }
        return Grid(alignment: .topLeading, horizontalSpacing: 12 * u, verticalSpacing: 12 * u) {
            ForEach(rows.indices, id: \.self) { r in
                GridRow {
                    ForEach(rows[r]) { s in MachineCard(s: s, hue: colors.hue(for: s.name), u: u, lines: activityLines) }
                    ForEach(0..<(n - rows[r].count), id: \.self) { _ in
                        Color.clear.frame(maxWidth: .infinity, maxHeight: 0)
                    }
                }
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Machine card

struct MachineCard: View {
    let s: StationVM
    let hue: Palette.RGB
    let u: CGFloat
    var lines = 3

    private var n: ClusterSnapshot.Node { s.node }

    private var dot: Color {
        if !s.live { return Palette.alert }
        if s.busy > 0 { return Palette.warm }
        if let st = s.minionState { return Palette.minionState(st).color() }
        return Color(red: 0.42, green: 0.95, blue: 0.62)
    }

    var body: some View {
        let mono = Font.system(size: 12.5 * u, design: .monospaced)
        VStack(alignment: .leading, spacing: 4 * u) {
            HStack(alignment: .firstTextBaseline) {
                Circle().fill(dot).frame(width: 9 * u, height: 9 * u)
                    .shadow(color: dot.opacity(0.8), radius: 4 * u)
                    .alignmentGuide(.firstTextBaseline) { d in d[.bottom] - 1 * u }
                Text(s.name)
                    .font(.system(size: 16 * u, weight: .semibold, design: .monospaced))
                    .foregroundStyle(hue.color()).lineLimit(1)
                    .layoutPriority(2)
                if let alias = NodeLines.alias(n) {
                    Text("(\(alias))")
                        .font(.system(size: 11.5 * u, design: .monospaced))
                        .foregroundStyle(hue.color(0.6)).lineLimit(1).truncationMode(.tail)
                }
                Spacer(minLength: 6 * u)
                if s.live {
                    let tok = n.tok_s_total ?? n.tok_s ?? 0
                    Text("\(Fmt.one(tok)) tok/s")
                        .font(mono).foregroundStyle(tok > 0 ? Palette.warm : .white.opacity(0.5))
                        .lineLimit(1).contentTransition(.numericText())
                }
            }
            Text(roleLine)
                .font(.system(size: 11 * u, weight: .medium)).tracking(1.5 * u)
                .foregroundStyle(roleColor.opacity(0.85)).lineLimit(1)
            NodeRoleLines(node: n, u: u, size: 12)
            Text("\(Fmt.chip(n.chip)) · \(Fmt.os(n.os)) · \(Fmt.gb(n.free_gb))/\(Fmt.gb(n.mem_gb)) GB")
                .font(.system(size: 11 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.45)).lineLimit(1)
            Text(s.moons.isEmpty ? "no models loaded" : s.moons.map { ModelHue.short($0.model) }.joined(separator: " · "))
                .font(.system(size: 11 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.38))
                .lineLimit(1).truncationMode(.tail)
            if let m = n.minion {
                Text(tonight(m.tonight)).font(mono).foregroundStyle(Palette.text.opacity(0.78)).lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            VStack(alignment: .leading, spacing: 3 * u) {
                let lines = activity
                if lines.isEmpty {
                    Text(s.live ? "  waiting for work…" : "  —").font(mono).foregroundStyle(.white.opacity(0.3))
                } else {
                    let shown = lines.count > self.lines ? max(1, self.lines - 1) : self.lines
                    ForEach(Array(lines.prefix(shown).enumerated()), id: \.offset) { i, l in
                        TypedLine(text: l, first: i == 0, u: u)
                    }
                    if lines.count > shown && self.lines > 1 {
                        Text("  + \(lines.count - shown) more").font(mono).foregroundStyle(.white.opacity(0.32))
                    }
                }
            }
            .frame(height: CGFloat(self.lines) * 18 * u, alignment: .topLeading)
            .clipped()
            .padding(.top, 2 * u)
        }
        .padding(.horizontal, 13 * u)
        .padding(.vertical, 12 * u)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(
            RoundedRectangle(cornerRadius: 14 * u, style: .continuous)
                .fill(Color.black.opacity(0.62))
                .overlay(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 2, style: .continuous)
                        .fill(hue.color(0.75)).frame(width: 3 * u).padding(.vertical, 14 * u)
                }
                .overlay(RoundedRectangle(cornerRadius: 14 * u, style: .continuous)
                    .strokeBorder(Color.white.opacity(0.08), lineWidth: 1))
        )
        .opacity(s.live ? 1 : 0.6)
        .contentShape(RoundedRectangle(cornerRadius: 14 * u, style: .continuous))
        #if os(iOS)
        // Long-press a card for the untruncated job + purpose.
        .contextMenu { Text(NodeLines.detail(n)) }
        #endif
    }

    private var roleLine: String {
        var parts: [String] = []
        if !s.live { parts.append("SILENT \(Fmt.ago(n.age_s ?? 0).uppercased())") }
        if s.isMinion { parts.append((s.minionState ?? "offline").uppercased()) }
        else { parts.append(s.serviceLabel.uppercased()) }
        parts.append("\(Fmt.compact(n.today?.tokens)) TODAY")
        return parts.joined(separator: "  ·  ")
    }

    private var roleColor: Color {
        if !s.live { return Palette.alert }
        return s.isMinion ? Palette.minionState(s.minionState).color() : Palette.cool
    }

    /// Live requests first, then what the minion is reading or transcribing.
    private var activity: [String] {
        var out = n.activeRequests.map { r in
            "\(ModelHue.short(r.model)) " + (r.generating ? "generating \(Fmt.int(r.decoded)) tok"
                                                          : "prefill \(Fmt.int(r.promptDone))/\(Fmt.int(r.prompt))")
        }
        for r in n.minion?.now_reading ?? [] {
            let phase = (r.phase ?? "reading").lowercased()
            let title = r.title ?? "…"
            out.append(phase == "transcribing" ? "transcribing: \(title)"
                       : phase == "reading" ? "reading: \(title)" : "\(phase): \(title)")
        }
        return out
    }

    private func tonight(_ t: ClusterSnapshot.Tonight?) -> String {
        let works = Int(t?.works ?? 0), lessons = Int(t?.lessons ?? 0), eps = Int(t?.transcribed ?? 0)
        if eps > 0 && works == 0 {
            return "tonight: \(Fmt.plural(eps, "episode", "episodes")) · \(Fmt.one(t?.audio_h)) h audio"
        }
        var s = "tonight: \(Fmt.plural(works, "work", "works")) · \(Fmt.plural(lessons, "lesson", "lessons"))"
        if eps > 0 { s += " · \(Fmt.plural(eps, "episode", "episodes"))" }
        return s
    }
}

/// A terminal line that types itself out once, with a blinking cursor on the first line.
struct TypedLine: View {
    let text: String
    let first: Bool
    let u: CGFloat
    @State private var shown = 0

    var body: some View {
        HStack(spacing: 0) {
            Text(first ? "› " : "  ").foregroundStyle(Palette.terminal.opacity(0.9))
            Text(String(text.prefix(shown)))
                .foregroundStyle(Palette.terminal.opacity(first ? 0.95 : 0.6))
            if first {
                TimelineView(.periodic(from: .now, by: 0.6)) { tl in
                    let on = Int(tl.date.timeIntervalSinceReferenceDate / 0.6) % 2 == 0
                    Text("▍").foregroundStyle(Palette.terminal.opacity(on ? 0.75 : 0))
                }
            }
        }
        .font(.system(size: 12.5 * u, design: .monospaced))
        .lineLimit(1)
        .truncationMode(.tail)
        .task(id: typedKey) {
            // Counters (prefill / generated tokens) just update; a new job types out.
            if shown > 0 && sameJob { shown = text.count; return }
            shown = 0
            for i in stride(from: 0, through: text.count, by: 1) {
                shown = i
                try? await Task.sleep(for: .milliseconds(24))
                if Task.isCancelled { return }
            }
        }
        .onChange(of: text) { old, new in
            lastPrefix = Self.jobKey(old)
            if Self.jobKey(new) == lastPrefix { shown = new.count }
        }
    }

    @State private var lastPrefix = ""
    private var typedKey: String { Self.jobKey(text) }
    private var sameJob: Bool { Self.jobKey(text) == lastPrefix }
    /// The part of a line that names the job (model + phase, or title) — digits excluded.
    static func jobKey(_ s: String) -> String { String(s.filter { !$0.isNumber && $0 != "," && $0 != "/" }) }
}

// MARK: - Lesson (large serif, as on Nova Minion)

struct DashLesson: View {
    let lessons: [ClusterSnapshot.Lesson]
    let colors: MachineColors
    let u: CGFloat
    let width: CGFloat
    /// Reports the lesson on stage (the constellation rings its star).
    var onCurrent: ((String?) -> Void)? = nil
    @State private var shown: ClusterSnapshot.Lesson?
    @State private var currentKey: String?
    @State private var visible = 0.0
    @State private var lastNewest: String?
    @State private var lastSwitch = Date()

    var body: some View {
        ZStack {
            if let l = shown {
                VStack(spacing: 14 * u) {
                    Text("\u{201C}\(LessonText.clean(l.lesson ?? ""))\u{201D}")
                        .font(.system(size: 36 * u, weight: .light, design: .serif))
                        .foregroundStyle(Palette.text)
                        .multilineTextAlignment(.center)
                        .lineSpacing(6 * u)
                        .lineLimit(3)
                        .minimumScaleFactor(0.55)
                        .shadow(color: .black.opacity(0.9), radius: 10 * u)
                    if let t = l.title {
                        Text("— from \(t)")
                            .font(.system(size: 18 * u, weight: .light)).italic()
                            .foregroundStyle(colors.hue(for: l.node).color(0.95))
                            .lineLimit(1).truncationMode(.middle)
                            .shadow(color: .black.opacity(0.9), radius: 6 * u)
                    }
                    Text(meta(l))
                        .font(.system(size: 11 * u, weight: .medium)).tracking(2.2 * u)
                        .foregroundStyle(.white.opacity(0.38))
                }
                .opacity(visible)
                .offset(y: (1 - visible) * 8 * u)
            } else {
                Text("Listening for Nova…")
                    .font(.system(size: 30 * u, weight: .light, design: .serif)).italic()
                    .foregroundStyle(Palette.text.opacity(0.6))
            }
        }
        .frame(width: width)
        .frame(minHeight: 130 * u, alignment: .bottom)
        .background(
            Rectangle()
                .fill(RadialGradient(colors: [Palette.space.opacity(0.85), Palette.space.opacity(0.5), Palette.space.opacity(0)],
                                     center: .center, startRadius: 0, endRadius: width * 0.62))
                .frame(width: width * 1.24, height: width * 1.24)
                .scaleEffect(x: 1, y: 0.4)
                .allowsHitTesting(false)
        )
        .onAppear { pickNewest(force: true) }
        .onChange(of: lessons.first?.key) { _, _ in pickNewest(force: false) }
        .task(id: currentKey) {
            if shown != nil, shown?.key != currentKey {
                withAnimation(.easeIn(duration: 1.1)) { visible = 0 }
                try? await Task.sleep(for: .seconds(1.15))
                if Task.isCancelled { return }
            }
            shown = lessons.first { $0.key == currentKey }
            onCurrent?(shown?.key)
            withAnimation(.easeOut(duration: 1.8)) { visible = 1 }
        }
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                guard Date().timeIntervalSince(lastSwitch) >= 22, lessons.count > 1 else { continue }
                let pool = Array(lessons.prefix(24))
                let i = (pool.firstIndex { $0.key == currentKey } ?? -1) + 1
                currentKey = pool[i % pool.count].key
                lastSwitch = Date()
            }
        }
    }

    private func pickNewest(force: Bool) {
        guard let newest = lessons.first?.key else { return }
        if force || newest != lastNewest {
            lastNewest = newest; currentKey = newest; lastSwitch = Date()
        }
    }

    private func meta(_ l: ClusterSnapshot.Lesson) -> String {
        var parts: [String] = []
        if let k = l.kind, !k.isEmpty { parts.append(k.replacingOccurrences(of: "_", with: " ").uppercased()) }
        parts.append(l.node.map { "LEARNED ON \($0.uppercased())" } ?? "LEARNED ON NOVA'S HUB")
        if let d = Fmt.date(l.at) { parts.append(Fmt.clock(d).uppercased()) }
        return parts.joined(separator: "  ·  ")
    }
}

// MARK: - Bottom: legend + totals

struct DashBottom: View {
    let snap: ClusterSnapshot?
    let colors: MachineColors
    let offline: Bool
    let lastSuccess: Date?
    let u: CGFloat

    var body: some View {
        VStack(spacing: 10 * u) {
            HStack(spacing: 16 * u) {
                ForEach(colors.order, id: \.self) { n in
                    HStack(spacing: 6 * u) {
                        Circle().fill(colors.hue(for: n).color()).frame(width: 7 * u, height: 7 * u)
                            .shadow(color: colors.hue(for: n).color(0.9), radius: 3 * u)
                        Text(n).font(.system(size: 12 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.5))
                    }
                }
            }
            .lineLimit(1).minimumScaleFactor(0.7)
            statusLine
        }
    }

    private var statusLine: some View {
        let sep = Text("   ·   ").foregroundStyle(.white.opacity(0.25))
        var line = Text("")
        if offline {
            let heard = lastSuccess.map { " · last heard \(Fmt.clock($0))" } ?? ""
            line = Text("hub unreachable\(heard)").foregroundStyle(Palette.warm.opacity(0.85))
            if snap != nil { line = line + sep }
        }
        if let s = snap {
            let t = s.totals
            let live = s.allNodes.filter(\.isLive).count
            line = line + Text("cluster: \(live)/\(s.allNodes.count) machines").foregroundStyle(Palette.warm.opacity(0.9))
                + sep + Text("\(Fmt.int(t?.models_loaded)) models · \(Fmt.gb(t?.vram_gb)) GB")
                + sep + Text("\(Fmt.int(t?.busy_slots)) active · \(Fmt.one(t?.tok_s_total ?? t?.tok_s)) tok/s · \(Fmt.int(t?.req_min)) req/min")
            let f = s.fleet_tonight
            line = line + sep + Text("\(Fmt.int(f?.works)) works · \(Fmt.int(f?.lessons)) lessons tonight")
                .foregroundStyle(Palette.terminal.opacity(0.85))
            if let eps = f?.transcribed, eps > 0 {
                line = line + sep + Text("\(Fmt.int(eps)) episodes (\(Fmt.one(f?.audio_h)) h)")
            }
            if let a = s.queue?["A"] {
                var still = "\(Fmt.int(a)) still to read"
                if let e = s.eta_days { still += " (~\(Int(e.rounded())) days)" }
                line = line + sep + Text(still)
            }
        }
        return line
            .font(.system(size: 14 * u))
            .foregroundStyle(.white.opacity(0.52))
            .lineLimit(1)
            .minimumScaleFactor(0.65)
    }
}
