//
//  Starfield.swift
//  Nova Minion (iPad + Mac)
//
//  Nova's constellation: every lesson is a star, colored by the minion that
//  learned it. Stars from the same work cluster and are joined by faint lines.
//  Three parallax depths drift on slow Lissajous paths. The star set is rebuilt
//  only when the fleet data changes; each frame just positions and draws it.
//

import SwiftUI

// MARK: - Palette

enum Palette {
    static let space = Color(red: 0.012, green: 0.014, blue: 0.030)
    static let text = Color(red: 0.93, green: 0.92, blue: 0.89)
    static let terminal = Color(red: 0.62, green: 0.86, blue: 0.80)
    static let warm = Color(red: 1.00, green: 0.74, blue: 0.42)

    /// One hue per minion, in fleet order. The first matches the screensaver's warm amber.
    static let nodeHues: [RGB] = [
        RGB(1.00, 0.74, 0.42),  // amber
        RGB(0.58, 0.76, 1.00),  // cool blue
        RGB(0.52, 0.92, 0.78),  // teal
        RGB(1.00, 0.56, 0.68),  // rose
        RGB(0.78, 0.64, 1.00),  // violet
        RGB(0.82, 0.93, 0.50),  // lime
    ]
    /// Lessons with no node (learned on the hub itself).
    static let hubHue = RGB(0.84, 0.86, 0.94)

    struct RGB: Equatable {
        var r, g, b: Double
        init(_ r: Double, _ g: Double, _ b: Double) { self.r = r; self.g = g; self.b = b }
        func color(_ a: Double = 1) -> Color { Color(red: r, green: g, blue: b, opacity: a) }
        func mixed(toWhite t: Double) -> RGB { RGB(r + (1 - r) * t, g + (1 - g) * t, b + (1 - b) * t) }
    }
}

/// Stable (per-string, across launches) hash so stars stay put between polls and restarts.
enum StableHash {
    static func of(_ s: String, seed: UInt64 = 0) -> UInt64 {
        var h: UInt64 = 0xcbf29ce484222325 ^ seed
        for b in s.utf8 { h ^= UInt64(b); h = h &* 0x100000001b3 }
        h ^= h >> 33; h = h &* 0xff51afd7ed558ccd; h ^= h >> 33
        return h
    }
    static func unit(_ s: String, _ seed: UInt64) -> Double {
        Double(of(s, seed: seed) >> 11) / Double(1 << 53)
    }
}

/// Maps node name → hue, consistently for the legend, cards, and stars.
struct NodeColors: Equatable {
    private(set) var order: [String] = []

    init(snapshot: FleetSnapshot?) {
        guard let s = snapshot else { return }
        for m in s.fleet where !order.contains(m.node) { order.append(m.node) }
        let extra = Set(s.lessons.compactMap(\.node)).subtracting(order).sorted()
        order += extra
    }

    func hue(for node: String?) -> Palette.RGB {
        guard let node, let i = order.firstIndex(of: node) else { return Palette.hubHue }
        return Palette.nodeHues[i % Palette.nodeHues.count]
    }
}

// MARK: - Star model

struct Star {
    let key: String
    let workKey: String
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
final class StarModel {
    private(set) var stars: [Star] = []
    /// Pairs of star indexes to join (same work).
    private(set) var links: [(Int, Int)] = []
    private var firstSeen: [String: Date] = [:]
    private var lastKeys: [String] = []
    private var lastColors: NodeColors?

    func update(lessons: [FleetSnapshot.Lesson], colors: NodeColors) {
        let keys = lessons.map(\.key)
        guard keys != lastKeys || colors != lastColors else { return }
        let initial = lastKeys.isEmpty
        lastKeys = keys; lastColors = colors

        let now = Date()
        var built: [Star] = []
        var seen = Set<String>()
        for (rank, l) in lessons.prefix(160).enumerated() {
            let key = l.key
            guard seen.insert(key).inserted else { continue }
            let work = l.title
            // Cluster center for the work, then a small scatter per lesson.
            let cx = 0.05 + 0.90 * StableHash.unit(work, 1)
            let cy = 0.10 + 0.82 * StableHash.unit(work, 2)
            let ang = StableHash.unit(key, 3) * 2 * .pi
            let rad = 0.015 + 0.055 * StableHash.unit(key, 4)
            let p = CGPoint(x: min(0.98, max(0.02, cx + cos(ang) * rad * 0.62)),
                            y: min(0.97, max(0.03, cy + sin(ang) * rad)))
            if firstSeen[key] == nil {
                // On first load, stagger the whole sky in; afterwards new stars fade in now.
                firstSeen[key] = initial ? now.addingTimeInterval(Double(rank) * 0.035) : now
            }
            built.append(Star(
                key: key, workKey: work,
                depth: Int(StableHash.of(work, seed: 5) % 3),
                norm: p,
                size: 1.4 + 2.0 * StableHash.unit(key, 6) + (rank < 8 ? 0.8 : 0),
                hue: colors.hue(for: l.node),
                twinkleRate: 0.25 + 0.9 * StableHash.unit(key, 7),
                twinklePhase: StableHash.unit(key, 8) * 2 * .pi,
                born: firstSeen[key]!,
                rank: rank))
        }
        // Chain stars of the same work in arrival order.
        var lastOfWork: [String: Int] = [:]
        var l: [(Int, Int)] = []
        for (i, s) in built.enumerated() {
            if let j = lastOfWork[s.workKey] { l.append((j, i)) }
            lastOfWork[s.workKey] = i
        }
        stars = built
        links = l
        let live = Set(keys)
        firstSeen = firstSeen.filter { live.contains($0.key) }
    }
}

// MARK: - Views

/// Faint static dust, drawn once per size change (not per frame).
struct DustLayer: View {
    var body: some View {
        Canvas(rendersAsynchronously: true) { ctx, size in
            let n = Int((size.width * size.height) / 3200)
            for i in 0..<n {
                let s = "dust\(i)"
                let x = StableHash.unit(s, 11) * size.width
                let y = StableHash.unit(s, 12) * size.height
                let a = 0.05 + 0.20 * pow(StableHash.unit(s, 13), 2)
                let r = 0.4 + 0.7 * StableHash.unit(s, 14)
                ctx.fill(Path(ellipseIn: CGRect(x: x, y: y, width: r * 2, height: r * 2)),
                         with: .color(Color(red: 0.80, green: 0.85, blue: 1.0, opacity: a)))
            }
        }
        .allowsHitTesting(false)
    }
}

struct StarfieldView: View {
    let model: StarModel
    let version: Int            // bump to signal model change
    let highlightKey: String?
    var paused: Bool

    private static let amps: [CGFloat] = [14, 26, 42]
    private static let periods: [Double] = [260, 190, 140]
    private static let phases: [Double] = [0.0, 2.1, 4.2]

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: paused)) { tl in
            Canvas { ctx, size in
                draw(&ctx, size: size, now: tl.date)
            }
        }
        .allowsHitTesting(false)
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

        // Field is slightly larger than the screen so drift never reveals an edge.
        let pad: CGFloat = 50 * unit
        func pos(_ s: Star) -> CGPoint {
            let o = offsets[s.depth]
            return CGPoint(x: -pad + s.norm.x * (size.width + 2 * pad) + o.width,
                           y: -pad + s.norm.y * (size.height + 2 * pad) + o.height)
        }
        func fade(_ s: Star) -> Double {
            let age = now.timeIntervalSince(s.born)
            if age <= 0 { return 0 }
            return min(1, age / 2.5)
        }
        let points = stars.map(pos)
        let fades = stars.map(fade)

        // Links between stars of the same work.
        var lines = Path()
        var hot = Path()
        for (a, b) in model.links {
            let f = min(fades[a], fades[b])
            guard f > 0.05 else { continue }
            if stars[a].key == highlightKey || stars[b].key == highlightKey {
                hot.move(to: points[a]); hot.addLine(to: points[b])
            } else {
                lines.move(to: points[a]); lines.addLine(to: points[b])
            }
        }
        ctx.stroke(lines, with: .color(Color(red: 0.86, green: 0.88, blue: 1.0, opacity: 0.13)), lineWidth: 0.8 * unit)
        ctx.stroke(hot, with: .color(Color(red: 1, green: 0.92, blue: 0.80, opacity: 0.32)), lineWidth: 1.0 * unit)

        // Stars: additive glow + bright core.
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
            let core = s.hue.mixed(toWhite: 0.3)
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - r * 0.75, y: p.y - r * 0.75, width: r * 1.5, height: r * 1.5)),
                     with: .color(core.color(0.95 * a)))
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
