//
//  ConstellationView.swift
//  Nova Cluster (iPad + Mac + Apple TV)
//
//  The third view (1.1.0): Nova Minion folded into Nova Cluster. Every lesson the
//  minions learned is a star, colored by the machine that learned it, and stars from
//  the same work are joined by faint lines. Minion cards across the top, the current
//  lesson in large serif, fleet totals along the bottom. Fed by the same /cluster
//  poll as the map and the dashboard; star placement is shared with the Nova Minion
//  screensaver (apps/Shared/NovaConstellationCore.swift).
//

import SwiftUI

// MARK: - Star model

struct LessonStar {
    let key: String
    let depth: Int
    let norm: CGPoint
    let size: Double
    let hue: Palette.RGB
    let twinkleRate: Double
    let twinklePhase: Double
    let born: Date
    let rank: Int          // 0 = newest
}

@MainActor
final class LessonStarModel {
    private(set) var stars: [LessonStar] = []
    /// Index pairs to join (same work).
    private(set) var links: [(Int, Int)] = []
    private var firstSeen: [String: Date] = [:]
    private var lastKeys: [String] = []
    private var lastColors: MachineColors?
    private var lastAspect: CGFloat = 0
    static let maxStars = 160

    /// `lessons` arrive newest first, as the hub sends them.
    func update(lessons: [ClusterSnapshot.Lesson], colors: MachineColors, aspect: CGFloat, now: Date = Date()) {
        let keys = lessons.map(\.key)
        let a = (aspect * 20).rounded() / 20          // ignore sub-5% resizes
        guard keys != lastKeys || colors != lastColors || a != lastAspect else { return }
        let initial = lastKeys.isEmpty
        lastKeys = keys; lastColors = colors; lastAspect = a

        var built: [LessonStar] = []
        var works: [String] = []
        var seen = Set<String>()
        for (rank, l) in lessons.prefix(Self.maxStars).enumerated() {
            let key = l.key
            guard seen.insert(key).inserted else { continue }
            let work = l.title ?? ""
            let p = ConstellationLayout.place(work: work, key: key, aspect: max(a, 0.3))
            if firstSeen[key] == nil {
                // First load staggers the whole sky in; afterwards new stars fade in now.
                firstSeen[key] = initial ? now.addingTimeInterval(Double(rank) * 0.035) : now
            }
            built.append(LessonStar(
                key: key, depth: p.depth, norm: p.norm,
                size: 1.4 + 2.0 * Double(p.sizeJitter) + (rank < 8 ? 0.8 : 0),
                hue: colors.hue(for: l.node),
                twinkleRate: 0.25 + 0.9 * Double(p.twinkle >> 11) / Double(1 << 53),
                twinklePhase: Double(p.twinkle & 0xFFFF) / 65536 * 2 * .pi,
                born: firstSeen[key]!, rank: rank))
            works.append(work)
        }
        // Chain stars of the same work, oldest to newest.
        let n = built.count
        let chain = ConstellationLayout.links(works: works.reversed())
        links = chain.map { (n - 1 - $0.0, n - 1 - $0.1) }
        stars = built
        let live = Set(keys)
        firstSeen = firstSeen.filter { live.contains($0.key) }
    }
}

// MARK: - Starfield

struct LessonStarfield: View {
    let model: LessonStarModel
    let version: Int            // bumped when the model changes
    let highlightKey: String?
    var paused: Bool

    private static let amps: [CGFloat] = [14, 26, 42]
    private static let periods: [Double] = [260, 190, 140]
    private static let phases: [Double] = [0.0, 2.1, 4.2]

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: paused)) { tl in
            Canvas { ctx, size in draw(&ctx, size: size, now: tl.date) }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private func offset(depth: Int, t: Double, unit: CGFloat) -> CGSize {
        let w = 2 * Double.pi / Self.periods[depth]
        let ph = Self.phases[depth]
        return CGSize(width: CGFloat(sin(t * w + ph)) * Self.amps[depth] * unit,
                      height: CGFloat(sin(t * w * 1.37 + ph * 0.7)) * Self.amps[depth] * 0.6 * unit)
    }

    private func draw(_ ctx: inout GraphicsContext, size: CGSize, now: Date) {
        let t = now.timeIntervalSinceReferenceDate
        let unit = max(0.7, min(size.width, size.height) / 820)
        let stars = model.stars
        guard !stars.isEmpty else { return }
        let offsets = (0..<3).map { offset(depth: $0, t: t, unit: unit) }
        // The field is slightly larger than the screen so drift never reveals an edge.
        let pad: CGFloat = 50 * unit
        let points = stars.map { s -> CGPoint in
            let o = offsets[s.depth]
            return CGPoint(x: -pad + s.norm.x * (size.width + 2 * pad) + o.width,
                           y: -pad + s.norm.y * (size.height + 2 * pad) + o.height)
        }
        let fades = stars.map { s -> Double in
            let age = now.timeIntervalSince(s.born)
            return age <= 0 ? 0 : min(1, age / 2.5)
        }

        var lines = Path(), hot = Path()
        for (a, b) in model.links where a < stars.count && b < stars.count {
            guard min(fades[a], fades[b]) > 0.05 else { continue }
            if stars[a].key == highlightKey || stars[b].key == highlightKey {
                hot.move(to: points[a]); hot.addLine(to: points[b])
            } else {
                lines.move(to: points[a]); lines.addLine(to: points[b])
            }
        }
        ctx.stroke(lines, with: .color(Color(red: 0.86, green: 0.88, blue: 1.0, opacity: 0.13)), lineWidth: 0.8 * unit)
        ctx.stroke(hot, with: .color(Color(red: 1, green: 0.92, blue: 0.80, opacity: 0.32)), lineWidth: 1.0 * unit)

        ctx.blendMode = .plusLighter
        for (i, s) in stars.enumerated() {
            let f = fades[i]
            guard f > 0 else { continue }
            let p = points[i]
            let tw = 0.78 + 0.22 * sin(t * s.twinkleRate * 2 + s.twinklePhase)
            let depthDim = [0.62, 0.82, 1.0][s.depth]
            let recency = s.rank < 12 ? 1.0 : max(0.55, 1.0 - Double(s.rank) / 260)
            let a = f * tw * depthDim * recency
            let r = CGFloat(s.size) * unit * CGFloat([0.8, 0.95, 1.15][s.depth])
            let glowR = r * 5.5
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - glowR, y: p.y - glowR, width: glowR * 2, height: glowR * 2)),
                     with: .radialGradient(Gradient(colors: [s.hue.color(0.75 * a), s.hue.color(0.18 * a), s.hue.color(0)]),
                                           center: p, startRadius: 0, endRadius: glowR))
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - r * 0.75, y: p.y - r * 0.75, width: r * 1.5, height: r * 1.5)),
                     with: .color(s.hue.mixed(toWhite: 0.3).color(0.95 * a)))
        }
        ctx.blendMode = .normal

        // Ring around the star whose lesson is on screen.
        if let hk = highlightKey, let i = stars.firstIndex(where: { $0.key == hk }), fades[i] > 0 {
            let p = points[i]
            let pulse = 0.5 + 0.5 * sin(t * 1.6)
            let rr = (15 + 4 * pulse) * unit
            ctx.stroke(Path(ellipseIn: CGRect(x: p.x - rr, y: p.y - rr, width: rr * 2, height: rr * 2)),
                       with: .color(stars[i].hue.color(0.30 + 0.25 * pulse)), lineWidth: 1.1 * unit)
        }
    }
}

// MARK: - Screen

struct ConstellationScreen<Mark: View>: View {
    let cluster: ClusterModel
    let stations: [StationVM]
    let paused: Bool
    @ViewBuilder let mark: (CGFloat) -> Mark
    @State private var stars = LessonStarModel()
    @State private var version = 0
    @State private var currentKey: String?

    private var snap: ClusterSnapshot? { cluster.snapshot }
    private var colors: MachineColors { MachineColors(stations: stations) }
    private var minions: [ClusterSnapshot.Node] { ConstellationFleet.minions(snap) }

    var body: some View {
        GeometryReader { geo in
            #if os(macOS)
            let u = max(0.8, min(geo.size.width / 1480, geo.size.height / 900))
            #elseif os(tvOS)
            let u = max(1.0, min(geo.size.width / 1440, geo.size.height / 900))
            #else
            let u = max(0.8, min(geo.size.width, geo.size.height) / 820)
            #endif
            let landscape = geo.size.width > geo.size.height
            ZStack {
                LessonStarfield(model: stars, version: version, highlightKey: currentKey, paused: paused)
                    .ignoresSafeArea()
                VStack(spacing: 0) {
                    VStack(alignment: .trailing, spacing: 12 * u) {
                        mark(u)
                        MinionStrip(minions: minions, colors: colors, u: u,
                                    columns: MinionStrip.columns(width: geo.size.width - 52 * u, u: u, landscape: landscape))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .padding(.horizontal, 26 * u)
                    .padding(.top, 14 * u)
                    Spacer(minLength: 8 * u)
                    DashLesson(lessons: snap?.lessons ?? [], colors: colors, u: u,
                               width: geo.size.width * (landscape ? 0.74 : 0.88),
                               onCurrent: { currentKey = $0 })
                        .padding(.bottom, (landscape ? 22 : 40) * u)
                    ConstellationBottom(snap: snap, minions: minions, colors: colors, offline: cluster.isOffline,
                                        lastSuccess: cluster.lastSuccess, u: u)
                        .padding(.horizontal, 26 * u)
                        .padding(.bottom, 16 * u)
                }
            }
            .onAppear { rebuild(aspect: geo.size.width / max(geo.size.height, 1)) }
            .onChange(of: snap) { _, _ in rebuild(aspect: geo.size.width / max(geo.size.height, 1)) }
            .onChange(of: geo.size) { _, s in rebuild(aspect: s.width / max(s.height, 1)) }
        }
    }

    private func rebuild(aspect: CGFloat) {
        stars.update(lessons: snap?.lessons ?? [], colors: colors, aspect: aspect)
        version &+= 1
    }
}

// MARK: - Minion cards

struct MinionStrip: View {
    let minions: [ClusterSnapshot.Node]
    let colors: MachineColors
    let u: CGFloat
    let columns: Int

    /// iPad: 3 landscape / 2 portrait. Mac and TV: as many ~250 pt cards as fit.
    static func columns(width: CGFloat, u: CGFloat, landscape: Bool) -> Int {
        #if os(iOS)
        return landscape ? 3 : 2
        #else
        return max(1, Int((width + 14 * u) / (250 * u + 14 * u)))
        #endif
    }

    var body: some View {
        if minions.isEmpty {
            HStack {
                Text("no minions reporting")
                    .font(.system(size: 14 * u, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.35))
                Spacer()
            }
        } else {
            let shown = Array(minions.prefix(12))   // the whole fleet (10 today); two rows on a Mac
            let n = max(1, min(shown.count, columns))
            let rows = stride(from: 0, to: shown.count, by: n).map { Array(shown[$0..<min($0 + n, shown.count)]) }
            Grid(alignment: .topLeading, horizontalSpacing: 14 * u, verticalSpacing: 14 * u) {
                ForEach(rows.indices, id: \.self) { r in
                    GridRow {
                        ForEach(rows[r]) { m in MinionCard(node: m, hue: colors.hue(for: m.node), u: u) }
                        ForEach(0..<(n - rows[r].count), id: \.self) { _ in
                            Color.clear.frame(maxWidth: .infinity, maxHeight: 0)
                        }
                    }
                }
            }
            .fixedSize(horizontal: false, vertical: true)
        }
    }
}

struct MinionCard: View {
    let node: ClusterSnapshot.Node
    let hue: Palette.RGB
    let u: CGFloat

    private var minion: ClusterSnapshot.Minion? { node.minion }
    private var state: String { (minion?.state ?? node.minion_state ?? "offline").lowercased() }

    var body: some View {
        let stateColor = Palette.minionState(state).color()
        let mono = Font.system(size: 13 * u, weight: .regular, design: .monospaced)
        VStack(alignment: .leading, spacing: 5 * u) {
            HStack(alignment: .firstTextBaseline) {
                Circle().fill(stateColor).frame(width: 9 * u, height: 9 * u)
                    .shadow(color: stateColor.opacity(0.8), radius: 4 * u)
                Text(NodeLines.clean(node.node))
                    .font(.system(size: 17 * u, weight: .semibold, design: .monospaced))
                    .foregroundStyle(hue.color())
                    .lineLimit(1)
                Spacer(minLength: 8 * u)
                if let t = minion?.tok_s, state != "offline" {
                    Text(String(format: "%.0f tok/s", t)).font(mono).foregroundStyle(.white.opacity(0.55)).lineLimit(1)
                }
            }
            Text(state.uppercased())
                .font(.system(size: 12 * u, weight: .medium)).tracking(1.6 * u)
                .foregroundStyle(stateColor.opacity(0.8)).lineLimit(1)
            if let os = node.os, !os.isEmpty {
                Text(Fmt.os(os)).font(.system(size: 12 * u, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.45)).lineLimit(1)
            }
            Text("tonight: " + ConstellationFleet.tonightParts(minion?.tonight).joined(separator: " · "))
                .font(mono).foregroundStyle(Palette.text.opacity(0.78))
                .lineLimit(1).minimumScaleFactor(0.72)
            let reading = minion?.now_reading ?? []
            VStack(alignment: .leading, spacing: 3 * u) {
                if reading.isEmpty {
                    Text(state == "offline" ? "  —" : "  waiting for work…").font(mono).foregroundStyle(.white.opacity(0.3))
                } else {
                    ForEach(Array(reading.prefix(3).enumerated()), id: \.offset) { i, r in
                        TypedLine(text: NodeLines.clean(ConstellationFleet.readingText(r)), first: i == 0, u: u)
                    }
                    if reading.count > 3 {
                        Text("  + \(reading.count - 3) more").font(mono).foregroundStyle(.white.opacity(0.32))
                    }
                }
            }
            .frame(minHeight: 3 * 19 * u, alignment: .topLeading)
            .padding(.top, 3 * u)
        }
        .padding(.horizontal, 13 * u)
        .padding(.vertical, 13 * u)
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
        .opacity(state == "offline" ? 0.6 : 1)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Bottom: legend + fleet totals

struct ConstellationBottom: View {
    let snap: ClusterSnapshot?
    let minions: [ClusterSnapshot.Node]
    let colors: MachineColors
    let offline: Bool
    let lastSuccess: Date?
    let u: CGFloat

    var body: some View {
        VStack(spacing: 10 * u) {
            HStack(spacing: 18 * u) {
                ForEach(minions) { m in
                    HStack(spacing: 6 * u) {
                        Circle().fill(colors.hue(for: m.node).color()).frame(width: 7 * u, height: 7 * u)
                            .shadow(color: colors.hue(for: m.node).color(0.9), radius: 3 * u)
                        Text(NodeLines.clean(m.node)).font(.system(size: 12 * u, design: .monospaced))
                            .foregroundStyle(.white.opacity(0.5))
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
            line = Text("Nova is resting\(heard)").foregroundStyle(Palette.warm.opacity(0.85))
            if snap != nil { line = line + sep }
        }
        if let s = snap {
            let f = s.fleet_tonight
            let m = Int(f?.minions ?? Double(minions.count))
            line = line + Text("fleet: \(Fmt.plural(m, "minion", "minions"))").foregroundStyle(Palette.warm.opacity(0.9))
                + sep + Text(Fmt.plural(Int(f?.works ?? 0), "work", "works"))
                + sep + Text("\(Fmt.plural(Int(f?.lessons ?? 0), "lesson", "lessons")) tonight")
            if let eps = f?.transcribed, eps > 0 {
                line = line + sep + Text("\(Fmt.plural(Int(eps), "episode", "episodes")) (\(Fmt.one(f?.audio_h)) h) transcribed")
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
