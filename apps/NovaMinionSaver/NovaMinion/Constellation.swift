//
//  Constellation.swift
//  Nova Minion screensaver
//
//  Nova's constellation: every lesson is a star. Stars from the same work
//  cluster together and are joined by faint lines; works are spread across
//  three parallax depths that drift independently. Steady stars are never
//  touched per frame — only depth containers, twinkle groups, and stars that
//  are arriving, fading in, or dying.
//

import AppKit
import QuartzCore

final class Constellation {
    static let maxStars = 300

    private final class Star {
        let key: String
        let workKey: String
        let mine: Bool
        let depth: Int
        let norm: CGPoint           // 0…1 in field space
        let baseSize: CGFloat
        let layer = CALayer()
        var rank = 0                // 0 = newest
        enum Phase { case fadingIn(CFTimeInterval), arriving(CFTimeInterval, CGPoint), settled, dying(CFTimeInterval) }
        var phase: Phase
        var trail: CALayer?

        init(key: String, workKey: String, mine: Bool, depth: Int, norm: CGPoint, baseSize: CGFloat, phase: Phase) {
            self.key = key; self.workKey = workKey; self.mine = mine
            self.depth = depth; self.norm = norm; self.baseSize = baseSize; self.phase = phase
        }
    }

    private struct Depth {
        let container = CALayer()
        let dust = CALayer()
        let coolLines = CAShapeLayer()
        let warmLines = CAShapeLayer()
        var groups: [CALayer] = []
        let amplitude: CGFloat      // points at 1080p
        let period: Double          // seconds for one Lissajous lap
        let phase: Double
    }

    let root = CALayer()
    private var depths: [Depth] = []
    private var stars: [Star] = []                 // oldest first
    private var byKey: [String: Star] = [:]
    private var pendingArrivals: [MinionLesson] = []
    private var lastArrivalLaunch: CFTimeInterval = 0
    private var size = CGSize(width: 1, height: 1)
    private var unit: CGFloat = 1                  // points per 1080p point
    private var scale: CGFloat = 2
    private var warmSprite: CGImage?
    private var coolSprite: CGImage?
    private var streakSprite: CGImage?
    private var linesDirty = false
    private let halo = CALayer()
    private var haloKey: String?
    private let twinkleGroups = 5

    init() {
        root.masksToBounds = true
        let specs: [(CGFloat, Double, Double)] = [(18, 240, 0.0), (34, 190, 1.7), (58, 150, 3.9)]
        for (i, spec) in specs.enumerated() {
            var d = Depth(amplitude: spec.0, period: spec.1, phase: spec.2)
            d.container.addSublayer(d.dust)
            for l in [d.coolLines, d.warmLines] {
                l.fillColor = nil
                l.lineWidth = 0.7
                l.lineCap = .round
                d.container.addSublayer(l)
            }
            d.coolLines.strokeColor = Palette.cool.withAlphaComponent(0.13 + 0.03 * CGFloat(i)).cgColor
            d.warmLines.strokeColor = Palette.warm.withAlphaComponent(0.17 + 0.03 * CGFloat(i)).cgColor
            for _ in 0..<twinkleGroups {
                let g = CALayer()
                d.container.addSublayer(g)
                d.groups.append(g)
            }
            root.addSublayer(d.container)
            depths.append(d)
        }
        halo.opacity = 0
        root.addSublayer(halo)
    }

    // MARK: - Layout

    func layout(size: CGSize, unit: CGFloat, scale: CGFloat) {
        guard size.width > 1, size.height > 1 else { return }
        let scaleChanged = scale != self.scale || warmSprite == nil
        self.size = size; self.unit = unit; self.scale = scale
        if scaleChanged {
            warmSprite = Render.starSprite(color: Palette.warm, scale: scale)
            coolSprite = Render.starSprite(color: Palette.cool, scale: scale)
            streakSprite = Render.streakSprite(scale: scale)
            halo.contents = Constellation.haloSprite(scale: scale)
        }
        noActions {
            root.frame = CGRect(origin: .zero, size: size)
            for (i, d) in depths.enumerated() {
                let pad = d.amplitude * unit * 1.5
                d.container.bounds = CGRect(origin: .zero, size: size)
                d.container.position = CGPoint(x: size.width / 2, y: size.height / 2)
                for g in d.groups { g.frame = d.container.bounds }
                d.coolLines.frame = d.container.bounds
                d.warmLines.frame = d.container.bounds
                d.dust.frame = d.container.bounds.insetBy(dx: -pad, dy: -pad)
                let area = size.width * size.height / (1920 * 1080)
                d.dust.contents = Render.dust(size: d.dust.frame.size, count: Int(CGFloat([420, 160, 50][i]) * max(area, 0.08)),
                                              seed: UInt64(41 + i), maxAlpha: [0.22, 0.32, 0.45][i] as CGFloat,
                                              maxRadius: [0.6, 0.9, 1.25][i] * max(unit, 0.5), scale: min(scale, 2))
                d.dust.contentsScale = min(scale, 2)
            }
            let hs = 70 * unit
            halo.bounds = CGRect(x: 0, y: 0, width: hs, height: hs)
            halo.contentsScale = scale
            for s in stars { place(s) }
        }
        linesDirty = true
    }

    // MARK: - Data

    /// Adds any lessons not yet in the field. `initial` places them instantly (staggered fade-in);
    /// otherwise each new one streaks in, one at a time.
    func ingest(_ lessons: [MinionLesson], initial: Bool, now: CFTimeInterval) {
        // Oldest first so ranks end up newest = 0.
        let pendingKeys = Set(pendingArrivals.map { $0.key })
        let fresh = lessons.reversed().filter { byKey[$0.key] == nil && !pendingKeys.contains($0.key) }
        if initial {
            for (i, l) in fresh.enumerated() { addStar(l, phase: .fadingIn(now + Double(i) * 0.025)) }
        } else {
            // A big backlog (e.g. waking after hours) would take minutes to streak in one by one:
            // the older ones simply fade in, the newest few get the shooting-star entrance.
            let streaking = 8
            let backlog = fresh.dropLast(min(streaking, fresh.count))
            for (i, l) in backlog.enumerated() { addStar(l, phase: .fadingIn(now + Double(i) * 0.03)) }
            pendingArrivals.append(contentsOf: fresh.suffix(streaking))
            if pendingArrivals.count > streaking * 2 { pendingArrivals.removeFirst(pendingArrivals.count - streaking * 2) }
        }
        rerank()
    }

    func position(ofLesson key: String) -> CGPoint? {
        guard let s = byKey[key] else { return nil }
        return screenPoint(of: s)
    }

    func highlight(_ key: String?) { haloKey = key }

    var count: Int { stars.count }

    // MARK: - Per frame

    func update(now t: CFTimeInterval) {
        // Launch queued arrivals, spaced out.
        if !pendingArrivals.isEmpty, t - lastArrivalLaunch > 0.9 {
            lastArrivalLaunch = t
            let lesson = pendingArrivals.removeFirst()
            if byKey[lesson.key] == nil {
                let s = addStar(lesson, phase: .settled)
                startArrival(s, now: t)
                rerank()
            }
        }

        noActions {
            for (i, d) in depths.enumerated() {
                let w = 2 * Double.pi / d.period
                let dx = CGFloat(sin(t * w + d.phase)) * d.amplitude * unit
                let dy = CGFloat(sin(t * w * 0.73 + d.phase * 1.3)) * d.amplitude * 0.6 * unit
                d.container.position = CGPoint(x: size.width / 2 + dx, y: size.height / 2 + dy)
                for (g, layer) in d.groups.enumerated() {
                    let rate = 0.35 + 0.17 * Double(g) + 0.05 * Double(i)
                    layer.opacity = Float(0.80 + 0.20 * sin(t * rate + Double(g) * 2.1 + Double(i)))
                }
            }

            var removed = false
            for s in stars {
                switch s.phase {
                case .settled:
                    continue
                case .fadingIn(let start):
                    let p = clamp((t - start) / 1.8)
                    s.layer.opacity = Float(p) * targetOpacity(s)
                    if p >= 1 { s.phase = .settled; linesDirty = true }
                case .arriving(let start, let from):
                    let p = clamp((t - start) / 2.4)
                    let e = 1 - pow(1 - p, 3)                       // ease-out cubic
                    let target = localPoint(of: s)
                    let pos = CGPoint(x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e)
                    s.layer.position = pos
                    let flare = 1 + 1.4 * (1 - CGFloat(e))
                    s.layer.bounds.size = CGSize(width: s.baseSize * unit * flare, height: s.baseSize * unit * flare)
                    s.layer.opacity = 1
                    if let trail = s.trail {
                        let remaining = hypot(target.x - pos.x, target.y - pos.y)
                        trail.position = pos
                        trail.bounds.size = CGSize(width: min(320 * unit, remaining * 0.9 + 40 * unit * CGFloat(1 - p)), height: 4 * unit)
                        trail.opacity = Float(1 - p)
                    }
                    if p >= 1 {
                        s.trail?.removeFromSuperlayer(); s.trail = nil
                        s.phase = .settled
                        place(s)
                        linesDirty = true
                    }
                case .dying(let start):
                    let p = clamp((t - start) / 3.0)
                    s.layer.opacity = Float(1 - p) * targetOpacity(s)
                    if p >= 1 { s.layer.removeFromSuperlayer(); removed = true }
                }
            }
            if removed {
                stars.removeAll { s in
                    if case .dying(let start) = s.phase, t - start >= 3.0 { byKey[s.key] = nil; return true }
                    return false
                }
                linesDirty = true
            }
            if linesDirty { rebuildLines(); linesDirty = false }

            // Halo pulse around the star whose lesson is on screen.
            if let k = haloKey, let s = byKey[k], case .settled = s.phase {
                halo.position = screenPoint(of: s)
                let pulse = 0.5 + 0.5 * sin(t * 1.6)
                halo.opacity = Float(0.35 + 0.35 * pulse)
                halo.transform = CATransform3DMakeScale(0.85 + 0.25 * pulse, 0.85 + 0.25 * pulse, 1)
            } else {
                halo.opacity = 0
            }
        }
    }

    // MARK: - Internals

    @discardableResult
    private func addStar(_ l: MinionLesson, phase: Star.Phase) -> Star {
        let workKey = l.title.lowercased()
        // Placement is shared with Nova Cluster's Constellation view (apps/Shared/NovaConstellationCore.swift).
        let p = ConstellationLayout.place(work: workKey, key: l.key, aspect: size.width / max(size.height, 1))
        let depth = p.depth
        let nx = p.norm.x, ny = p.norm.y
        let base = (l.mine ? 22 : 17) + p.sizeJitter * 11
        let s = Star(key: l.key, workKey: workKey, mine: l.mine, depth: depth,
                     norm: CGPoint(x: nx, y: ny), baseSize: base, phase: phase)
        s.layer.contents = l.mine ? warmSprite : coolSprite
        s.layer.contentsScale = scale
        noActions {
            place(s)
            if case .fadingIn = phase { s.layer.opacity = 0 }
            let group = Int(p.twinkle % UInt64(twinkleGroups))
            depths[depth].groups[group].addSublayer(s.layer)
        }
        stars.append(s)
        byKey[s.key] = s
        // Keep the field bounded: the oldest beyond the cap fade away.
        let living = stars.filter { if case .dying = $0.phase { return false }; return true }
        if living.count > Constellation.maxStars {
            let now = CACurrentMediaTime()
            for old in living.prefix(living.count - Constellation.maxStars) { old.phase = .dying(now) }
        }
        return s
    }

    private func startArrival(_ s: Star, now: CFTimeInterval) {
        let target = localPoint(of: s)
        var r = SeededRandom(seed: StableHash.fnv1a(s.key) ^ 0xA11)
        // Enter from the upper sky at a shallow, slightly random angle.
        let angle = CGFloat.pi * (0.62 + 0.5 * r.next())         // pointing up-left … up-right
        let dist = max(size.width, size.height) * 0.55
        let from = CGPoint(x: target.x + cos(angle) * dist * (r.next() < 0.5 ? 1 : -1), y: target.y + abs(sin(angle)) * dist)
        s.phase = .arriving(now, from)
        noActions {
            s.layer.position = from
            s.layer.opacity = 1
            let trail = CALayer()
            trail.contents = streakSprite
            trail.contentsScale = scale
            trail.anchorPoint = CGPoint(x: 1, y: 0.5)
            trail.position = from
            trail.bounds = CGRect(x: 0, y: 0, width: 1, height: 4 * unit)
            let heading = atan2(target.y - from.y, target.x - from.x)
            trail.setAffineTransform(CGAffineTransform(rotationAngle: heading))
            s.layer.superlayer?.insertSublayer(trail, below: s.layer)
            s.trail = trail
        }
    }

    private func rerank() {
        var rank = 0
        noActions {
            for s in stars.reversed() {
                if case .dying = s.phase { continue }
                s.rank = rank; rank += 1
                if case .settled = s.phase { s.layer.opacity = targetOpacity(s) }
            }
        }
    }

    private func targetOpacity(_ s: Star) -> Float {
        // Newest stars burn brightest; the old sky dims gently.
        let r = Float(s.rank)
        return max(0.38, 1.0 - r / Float(Constellation.maxStars) * 0.62)
    }

    private func localPoint(of s: Star) -> CGPoint {
        CGPoint(x: s.norm.x * size.width, y: s.norm.y * size.height)
    }

    private func screenPoint(of s: Star) -> CGPoint {
        let d = depths[s.depth].container.position
        let p = localPoint(of: s)
        return CGPoint(x: p.x + d.x - size.width / 2, y: p.y + d.y - size.height / 2)
    }

    private func place(_ s: Star) {
        if case .arriving = s.phase { return }
        s.layer.bounds = CGRect(x: 0, y: 0, width: s.baseSize * unit, height: s.baseSize * unit)
        s.layer.position = localPoint(of: s)
    }

    /// Joins each settled star to its nearest earlier sibling from the same work.
    private func rebuildLines() {
        let cool = [CGMutablePath(), CGMutablePath(), CGMutablePath()]
        let warm = [CGMutablePath(), CGMutablePath(), CGMutablePath()]
        var seen: [String: [Star]] = [:]
        for s in stars {
            guard case .settled = s.phase else { continue }
            let p = localPoint(of: s)
            if let siblings = seen[s.workKey] {
                var best: Star?
                var bestD = CGFloat.greatestFiniteMagnitude
                for o in siblings {
                    let q = localPoint(of: o)
                    let d = hypot(q.x - p.x, q.y - p.y)
                    if d < bestD { bestD = d; best = o }
                }
                if let b = best {
                    let q = localPoint(of: b)
                    // Trim ends so the line doesn't cut through the glow cores.
                    let trim = min(bestD * 0.3, 5 * unit)
                    let ux = (q.x - p.x) / max(bestD, 0.001), uy = (q.y - p.y) / max(bestD, 0.001)
                    let path = (s.mine && b.mine) ? warm[s.depth] : cool[s.depth]
                    path.move(to: CGPoint(x: p.x + ux * trim, y: p.y + uy * trim))
                    path.addLine(to: CGPoint(x: q.x - ux * trim, y: q.y - uy * trim))
                }
            }
            seen[s.workKey, default: []].append(s)
        }
        for i in 0..<depths.count {
            depths[i].coolLines.path = cool[i]
            depths[i].warmLines.path = warm[i]
            depths[i].coolLines.lineWidth = 0.7 * max(unit, 0.5)
            depths[i].warmLines.lineWidth = 0.7 * max(unit, 0.5)
        }
    }

    private static func haloSprite(scale: CGFloat) -> CGImage? {
        let d: CGFloat = 70
        return Render.image(size: CGSize(width: d, height: d), scale: scale) { ctx in
            let c = CGPoint(x: d / 2, y: d / 2)
            for (r, a, w) in [(22.0, 0.10, 6.0), (22.0, 0.55, 0.8), (31.0, 0.18, 0.6)] as [(CGFloat, CGFloat, CGFloat)] {
                ctx.setStrokeColor(CGColor(red: 1, green: 0.92, blue: 0.80, alpha: a))
                ctx.setLineWidth(w)
                ctx.strokeEllipse(in: CGRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r))
            }
        }
    }
}

@inline(__always) func clamp(_ x: Double) -> Double { min(1, max(0, x)) }

@inline(__always) func noActions(_ body: () -> Void) {
    CATransaction.begin()
    CATransaction.setDisableActions(true)
    body()
    CATransaction.commit()
}
