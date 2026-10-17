//
//  ClusterMap.swift
//  Nova Cluster (iPad + Mac)
//
//  The cluster map: every machine is a station, mac-studio (Nova's hub) at the center,
//  the rest on a ring. A station's loaded models orbit it as moons sized by memory and
//  colored by model family. A busy slot makes its moon pulse — a filling arc while it
//  reads the prompt (prefill), a spinning ring + token count while it generates. Every
//  finished request fires a comet from its station back to the hub, sized by tokens.
//
//  The station set is rebuilt only when a poll changes it; each frame (30 fps, Canvas +
//  TimelineView) just positions and draws.
//

import SwiftUI

// MARK: - View models

struct MoonVM {
    var key: String
    var model: String
    var gb: Double
    var vram: Double
    var ctx: Int
    var port: Int
    var hue: Palette.RGB
    var ring: Int
    var slot: Int
    var ringCount: Int
    var active: [ActiveRequest]
    var radius: CGFloat { CGFloat(2.6 + 1.55 * sqrt(max(0.1, gb))) }
}

struct StationVM: Identifiable {
    var name: String
    var isHub: Bool
    var node: ClusterSnapshot.Node
    var moons: [MoonVM]
    var hue: Palette.RGB
    var phase: Double
    var omega: Double
    var id: String { name }
    var live: Bool { node.isLive }
    var busy: Int { node.activeRequests.count }
    var isMinion: Bool { node.minion != nil }
    var minionState: String? { node.minion == nil ? nil : (node.minion?.state ?? node.minion_state) }

    /// What a plain (non-minion) LLM node is for.
    var serviceLabel: String {
        let names = (node.loaded ?? []).map(\.name) + (node.runners ?? []).compactMap(\.model)
        if names.isEmpty { return "standby" }
        if names.allSatisfy({ $0.lowercased().contains("embed") }) { return "embeddings" }
        return "fast pool"
    }
}

@MainActor
final class MapModel {
    private(set) var stations: [StationVM] = []

    func update(_ snap: ClusterSnapshot?) {
        let nodes = snap?.allNodes ?? []
        guard !nodes.isEmpty else { stations = []; return }
        let hubName = nodes.first { $0.node == "mac-studio" }?.node
            ?? nodes.max { ($0.vram_gb ?? 0) < ($1.vram_gb ?? 0) }!.node
        let ring = nodes.filter { $0.node != hubName }.sorted { a, b in
            let am = (a.os ?? "").lowercased().contains("macos"), bm = (b.os ?? "").lowercased().contains("macos")
            if am != bm { return am }
            return a.node.localizedStandardCompare(b.node) == .orderedAscending
        }
        let ordered = nodes.filter { $0.node == hubName } + ring
        stations = ordered.map { n in
            let hub = n.node == hubName
            return StationVM(name: n.node, isHub: hub, node: n, moons: Self.moons(for: n, hub: hub),
                             hue: Palette.station(os: n.os, hub: hub),
                             phase: StableHash.unit(n.node, 21) * 2 * .pi,
                             omega: 2 * .pi / (70 + 50 * StableHash.unit(n.node, 22)))
        }
    }

    private static func moons(for n: ClusterSnapshot.Node, hub: Bool) -> [MoonVM] {
        var list: [MoonVM] = []
        var seen = Set<String>()
        for l in n.loaded ?? [] {
            let key = "\(n.node)|\(l.name)|\(Int(l.port ?? 0))"
            guard seen.insert(key).inserted else { continue }
            let gb = max(l.size_gb ?? 0, l.vram_gb ?? 0)
            list.append(MoonVM(key: key, model: l.name, gb: gb, vram: l.vram_gb ?? 0, ctx: Int(l.ctx ?? 0),
                               port: Int(l.port ?? 0), hue: ModelHue.of(l.name), ring: 0, slot: 0, ringCount: 1, active: []))
        }
        // Standalone runners (e.g. a llama-server outside Ollama) appear as moons of their own.
        for r in n.runners ?? [] {
            guard let m = r.model, !list.contains(where: { $0.model == m }) else { continue }
            let key = "\(n.node)|\(m)|r\(Int(r.port ?? 0))"
            guard seen.insert(key).inserted else { continue }
            list.append(MoonVM(key: key, model: m, gb: 4, vram: 0, ctx: Int(r.ctx ?? 0), port: Int(r.port ?? 0),
                               hue: ModelHue.of(m), ring: 0, slot: 0, ringCount: 1, active: []))
        }
        // Busy slots → the first moon carrying that model.
        for a in n.activeRequests {
            if let i = list.firstIndex(where: { $0.model == a.model }) { list[i].active.append(a) }
        }
        // Stable order; the hub splits big models onto an outer ring.
        list.sort { $0.gb == $1.gb ? $0.key < $1.key : $0.gb > $1.gb }
        if hub && list.count > 4 {
            let outer = (list.count + 1) / 2
            for i in list.indices { list[i].ring = i < outer ? 1 : 0 }
        }
        for ring in 0...1 {
            let idx = list.indices.filter { list[$0].ring == ring }
            for (k, i) in idx.enumerated() { list[i].slot = k; list[i].ringCount = idx.count }
        }
        return list
    }
}

// MARK: - Geometry

struct MapGeometry {
    let size: CGSize
    let u: CGFloat
    let center: CGPoint
    let points: [String: CGPoint]
    /// Upper side stations carry their text above them, so vertical neighbours never collide.
    let above: Set<String>

    init(size: CGSize, stations: [StationVM]) {
        self.size = size
        let u = max(0.62, min(1.5, min(size.width / 1040, size.height / 760)))
        self.u = u
        let c = CGPoint(x: size.width / 2, y: size.height * 0.5)
        center = c
        var pts: [String: CGPoint] = [:]
        var up = Set<String>()
        let ring = stations.filter { !$0.isHub }
        let n = max(1, ring.count)
        // Labels sit outside the ring (above for the upper half, below for the lower),
        // so the ring keeps ~165 pt of headroom for them top and bottom (7 fixed lines).
        let rx = max(160 * u, size.width / 2 - 118 * u)
        let ry = max(150 * u, (size.height * 0.5 - 164 * u) / 0.92)
        for (i, s) in ring.enumerated() {
            let a = (-90 + 180 / Double(n) + Double(i) * 360 / Double(n)) * .pi / 180
            pts[s.name] = CGPoint(x: c.x + rx * CGFloat(cos(a)), y: c.y + ry * CGFloat(sin(a)))
            if sin(a) < 0 { up.insert(s.name) }
        }
        above = up
        for s in stations where s.isHub { pts[s.name] = c }
        points = pts
    }

    func coreRadius(_ s: StationVM) -> CGFloat { (s.isHub ? 15 : 8.5) * u }
    func orbit(_ s: StationVM, ring: Int) -> CGFloat {
        s.isHub ? (ring == 1 ? 104 : 56) * u : 34 * u
    }
    /// Where the station's text block attaches: just past its outermost moon,
    /// below the station (or above it, for upper side stations).
    func labelAnchor(_ s: StationVM) -> (CGPoint, Bool) {
        let p = points[s.name] ?? center
        let outer = s.moons.map { orbit(s, ring: $0.ring) + $0.radius * u }.max() ?? coreRadius(s) + 10 * u
        let d = max(outer, coreRadius(s) + 12 * u) + 8 * u
        let up = above.contains(s.name)
        return (CGPoint(x: p.x, y: up ? p.y - d : p.y + d), up)
    }

    func moonAngle(_ s: StationVM, _ m: MoonVM, t: Double) -> Double {
        let dir = m.ring == 1 ? -0.65 : 1.0
        return s.phase + Double(m.ring) * 0.4 + Double(m.slot) * 2 * .pi / Double(max(1, m.ringCount)) + t * s.omega * dir
    }

    func moonPoint(_ s: StationVM, _ m: MoonVM, t: Double) -> CGPoint {
        let p = points[s.name] ?? center
        let a = moonAngle(s, m, t: t)
        let r = orbit(s, ring: m.ring)
        return CGPoint(x: p.x + r * CGFloat(cos(a)), y: p.y + r * CGFloat(sin(a)))
    }
}

/// Pointer state shared with the renderer (not observed: the map redraws at 30 fps anyway).
@MainActor
final class MapPointer {
    var hover: CGPoint?
    var tap: CGPoint?
    var selectedKey: String?
    var selectedUntil = Date.distantPast
}

// MARK: - Renderer

struct ClusterMapView: View {
    let map: MapModel
    let comets: CometQueue
    let pointer: MapPointer
    let version: Int
    var dimmed: Bool
    var paused: Bool

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: paused)) { tl in
            Canvas { ctx, size in
                draw(&ctx, size: size, now: tl.date)
            }
        }
        .drawingGroup()
    }

    private static let cometLife = 2.3
    private static let stationKey = "station:"

    private func draw(_ ctx: inout GraphicsContext, size: CGSize, now: Date) {
        let stations = map.stations
        guard !stations.isEmpty else { return }
        let t = now.timeIntervalSinceReferenceDate
        let g = MapGeometry(size: size, stations: stations)
        let u = g.u
        if dimmed { ctx.opacity = 0.55 }

        // Spokes: hub → every station. A station serving a request gets a flowing dash.
        for s in stations where !s.isHub {
            guard let p = g.points[s.name] else { continue }
            var path = Path(); path.move(to: g.center); path.addLine(to: p)
            ctx.stroke(path, with: .color(.white.opacity(s.live ? 0.07 : 0.03)), lineWidth: 1 * u)
            if s.busy > 0 {
                ctx.stroke(path, with: .color(s.hue.color(0.45)),
                           style: StrokeStyle(lineWidth: 1.4 * u, lineCap: .round, dash: [2 * u, 9 * u],
                                              dashPhase: CGFloat(-t * 26) * u))
            }
        }

        // Orbit tracks.
        for s in stations {
            guard let p = g.points[s.name] else { continue }
            let rings = Set(s.moons.map(\.ring))
            for r in rings {
                let rr = g.orbit(s, ring: r)
                ctx.stroke(Path(ellipseIn: CGRect(x: p.x - rr, y: p.y - rr, width: rr * 2, height: rr * 2)),
                           with: .color(.white.opacity(s.live ? 0.06 : 0.025)), lineWidth: 0.8 * u)
            }
        }

        // Stations.
        for s in stations {
            guard let p = g.points[s.name] else { continue }
            drawStation(&ctx, s, at: p, g: g, t: t)
        }

        // Moons.
        var hits: [(MoonVM, StationVM, CGPoint)] = []
        for s in stations {
            for m in s.moons {
                let mp = g.moonPoint(s, m, t: t)
                hits.append((m, s, mp))
                drawMoon(&ctx, m, s, at: mp, g: g, t: t)
            }
        }

        // Comets.
        comets.prune(now: now, life: Self.cometLife)
        ctx.blendMode = .plusLighter
        for c in comets.items {
            let age = now.timeIntervalSince(c.born)
            guard age >= 0, let p = g.points[c.node] else { continue }
            let hub = stations.first { $0.name == c.node }?.isHub ?? false
            drawComet(&ctx, c, from: p, hub: hub, g: g, age: age)
        }
        ctx.blendMode = .normal

        // Hover (Mac) / tap (iPad): ring + tooltip on the nearest moon.
        resolvePointer(hits: hits, g: g, now: now)
        if let key = pointer.selectedKey, let h = hits.first(where: { $0.0.key == key }) {
            drawTooltip(&ctx, h.0, h.1, at: h.2, g: g, size: size)
        } else if let key = pointer.selectedKey, key.hasPrefix(Self.stationKey),
                  let s = stations.first(where: { Self.stationKey + $0.name == key }), let p = g.points[s.name] {
            drawStationTooltip(&ctx, s, at: p, g: g, size: size)
        }
    }

    private func resolvePointer(hits: [(MoonVM, StationVM, CGPoint)], g: MapGeometry, now: Date) {
        func nearest(_ q: CGPoint) -> String? {
            var best: (String, CGFloat)?
            for (m, _, p) in hits {
                let d = hypot(p.x - q.x, p.y - q.y) - m.radius * g.u
                if d < 18 * g.u, d < (best?.1 ?? .infinity) { best = (m.key, d) }
            }
            if best == nil {
                // Not on a moon: the station itself (its full LLM job + purpose).
                for s in map.stations {
                    guard let p = g.points[s.name] else { continue }
                    let d = hypot(p.x - q.x, p.y - q.y) - g.coreRadius(s)
                    if d < 16 * g.u, d < (best?.1 ?? .infinity) { best = (Self.stationKey + s.name, d) }
                }
            }
            return best?.0
        }
        if let tap = pointer.tap {
            pointer.tap = nil
            pointer.selectedKey = nearest(tap)
            pointer.selectedUntil = now.addingTimeInterval(6)
        } else if let h = pointer.hover {
            pointer.selectedKey = nearest(h)
            pointer.selectedUntil = now.addingTimeInterval(0.2)
        } else if now > pointer.selectedUntil {
            pointer.selectedKey = nil
        }
    }

    private func drawStation(_ ctx: inout GraphicsContext, _ s: StationVM, at p: CGPoint, g: MapGeometry, t: Double) {
        let u = g.u
        let a = s.live ? 1.0 : 0.32
        let cr = g.coreRadius(s)
        let busyPulse = s.busy > 0 ? 0.5 + 0.5 * sin(t * 3.2) : 0
        let glowR = cr * (s.isHub ? 4.6 : 4.2) * CGFloat(1 + 0.18 * busyPulse)
        ctx.fill(Path(ellipseIn: CGRect(x: p.x - glowR, y: p.y - glowR, width: glowR * 2, height: glowR * 2)),
                 with: .radialGradient(Gradient(colors: [s.hue.color((0.42 + 0.25 * busyPulse) * a),
                                                         s.hue.color(0.10 * a), s.hue.color(0)]),
                                       center: p, startRadius: 0, endRadius: glowR))
        let core = s.hue.mixed(toWhite: 0.35).color(0.95 * a)
        if s.isMinion || s.isHub {
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - cr, y: p.y - cr, width: cr * 2, height: cr * 2)), with: .color(core))
        } else {
            // Service node: a diamond, no minion ring.
            var d = Path()
            d.move(to: CGPoint(x: p.x, y: p.y - cr * 1.15)); d.addLine(to: CGPoint(x: p.x + cr * 1.15, y: p.y))
            d.addLine(to: CGPoint(x: p.x, y: p.y + cr * 1.15)); d.addLine(to: CGPoint(x: p.x - cr * 1.15, y: p.y))
            d.closeSubpath()
            ctx.fill(d, with: .color(core))
        }
        if s.isMinion {
            // Minion ring by state: dedicated solid, idle dashed, away dotted.
            let st = (s.minionState ?? "offline").lowercased()
            let col = Palette.minionState(st)
            let rr = cr + 5 * u
            let dash: [CGFloat] = st == "dedicated" ? [] : st == "idle" ? [4 * u, 3 * u] : [1 * u, 4 * u]
            ctx.stroke(Path(ellipseIn: CGRect(x: p.x - rr, y: p.y - rr, width: rr * 2, height: rr * 2)),
                       with: .color(col.color(0.85 * a)),
                       style: StrokeStyle(lineWidth: 1.6 * u, lineCap: .round, dash: dash,
                                          dashPhase: st == "dedicated" ? 0 : CGFloat(t * 4) * u))
        }
    }

    private func drawMoon(_ ctx: inout GraphicsContext, _ m: MoonVM, _ s: StationVM, at p: CGPoint, g: MapGeometry, t: Double) {
        let u = g.u
        let a = s.live ? 1.0 : 0.3
        let busy = !m.active.isEmpty
        let pulse = busy ? 0.5 + 0.5 * sin(t * 4.2) : 0
        let r = m.radius * u * CGFloat(1 + 0.14 * pulse)
        let glowR = r * (busy ? 3.6 : 2.4)
        ctx.fill(Path(ellipseIn: CGRect(x: p.x - glowR, y: p.y - glowR, width: glowR * 2, height: glowR * 2)),
                 with: .radialGradient(Gradient(colors: [m.hue.color((busy ? 0.55 + 0.3 * pulse : 0.28) * a), m.hue.color(0)]),
                                       center: p, startRadius: r * 0.6, endRadius: glowR))
        ctx.fill(Path(ellipseIn: CGRect(x: p.x - r, y: p.y - r, width: r * 2, height: r * 2)),
                 with: .radialGradient(Gradient(colors: [m.hue.mixed(toWhite: 0.55).color(a), m.hue.color(0.85 * a)]),
                                       center: CGPoint(x: p.x - r * 0.35, y: p.y - r * 0.35), startRadius: 0, endRadius: r * 1.4))
        // CPU-only model (no VRAM): a hollow center reads as "on the CPU".
        if m.vram <= 0 && m.gb > 0 {
            let h = r * 0.42
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - h, y: p.y - h, width: h * 2, height: h * 2)),
                     with: .color(Palette.space.opacity(0.75 * a)))
        }

        let ang = g.moonAngle(s, m, t: t)
        var label: String?
        var labelColor = Color.white.opacity(0.42 * a)
        if let req = m.active.first {
            let rr = r + 4.5 * u
            let ring = CGRect(x: p.x - rr, y: p.y - rr, width: rr * 2, height: rr * 2)
            if req.generating {
                for k in 0..<3 {
                    let start = t * 3.4 + Double(k) * 2 * .pi / 3
                    var arc = Path()
                    arc.addArc(center: p, radius: rr, startAngle: .radians(start), endAngle: .radians(start + 0.95), clockwise: false)
                    ctx.stroke(arc, with: .color(m.hue.mixed(toWhite: 0.5).color(0.95)), style: StrokeStyle(lineWidth: 1.8 * u, lineCap: .round))
                }
                let total = m.active.reduce(0) { $0 + $1.decoded }
                label = "▸ \(Fmt.int(total)) tok"
            } else {
                ctx.stroke(Path(ellipseIn: ring), with: .color(.white.opacity(0.14)), lineWidth: 1.8 * u)
                var arc = Path()
                let f = max(0.03, req.prefillFraction)
                arc.addArc(center: p, radius: rr, startAngle: .degrees(-90), endAngle: .degrees(-90 + 360 * f), clockwise: false)
                ctx.stroke(arc, with: .color(Palette.warm.opacity(0.95)), style: StrokeStyle(lineWidth: 2.2 * u, lineCap: .round))
                label = "prefill \(Int(req.prefillFraction * 100))%"
            }
            if m.active.count > 1 { label! += "  ×\(m.active.count)" }
            labelColor = m.hue.mixed(toWhite: 0.4).color(0.95)
        } else if s.isHub && m.ring == 1 {
            label = ModelHue.short(m.model)
        }
        if let label {
            let off = r + (busy ? 10 : 6) * u
            let c = cos(ang), sn = sin(ang)
            let lp = CGPoint(x: p.x + off * CGFloat(c), y: p.y + off * CGFloat(sn))
            // The hub's own label sits right below it, so its moons' tags go sideways, never down.
            let anchor: UnitPoint = s.isHub ? (c >= 0 ? .leading : .trailing)
                : c > 0.35 ? .leading : c < -0.35 ? .trailing : (sn > 0 ? .top : .bottom)
            ctx.draw(Text(label).font(.system(size: (busy ? 11 : 10) * u, weight: busy ? .semibold : .regular, design: .monospaced))
                        .foregroundColor(labelColor), at: lp, anchor: anchor)
        }
    }

    private func drawComet(_ ctx: inout GraphicsContext, _ c: Comet, from p: CGPoint, hub: Bool, g: MapGeometry, age: Double) {
        let u = g.u
        let hue = ModelHue.of(c.model)
        let mag = log2(Double(max(1, c.tokens)) + 1)           // 1 … ~14
        let head = CGFloat(1.6 + 0.42 * mag) * u
        // Burst of sparks at the station.
        let burstLife = 0.9
        if age < burstLife {
            let k = 4 + Int(mag / 1.4)
            let f = age / burstLife
            let e = 1 - pow(1 - f, 3)
            for i in 0..<k {
                let ang = StableHash.unit(c.key, UInt64(30 + i)) * 2 * .pi
                let dist = CGFloat(e) * (14 + CGFloat(mag) * 3.2) * u
                let q = CGPoint(x: p.x + dist * CGFloat(cos(ang)), y: p.y + dist * CGFloat(sin(ang)))
                let r = 1.4 * u
                ctx.fill(Path(ellipseIn: CGRect(x: q.x - r, y: q.y - r, width: r * 2, height: r * 2)),
                         with: .color(hue.mixed(toWhite: 0.4).color(0.9 * (1 - f))))
            }
        }
        if hub {
            // The hub's own requests: a shock ring.
            let f = min(1, age / 1.5)
            let rr = (18 + CGFloat(f) * (40 + CGFloat(mag) * 5)) * u
            ctx.stroke(Path(ellipseIn: CGRect(x: p.x - rr, y: p.y - rr, width: rr * 2, height: rr * 2)),
                       with: .color(hue.color(0.55 * (1 - f))), lineWidth: (1 + CGFloat(mag) * 0.18) * u)
            return
        }
        // Comet: station → hub on a gentle curve, tail behind.
        let travel = 1.6
        let a = g.center
        let mid = CGPoint(x: (p.x + a.x) / 2, y: (p.y + a.y) / 2)
        let dx = a.x - p.x, dy = a.y - p.y
        let len = max(1, hypot(dx, dy))
        let side: CGFloat = StableHash.unit(c.key, 9) > 0.5 ? 1 : -1
        let ctrl = CGPoint(x: mid.x - dy / len * len * 0.16 * side, y: mid.y + dx / len * len * 0.16 * side)
        func at(_ s: Double) -> CGPoint {
            let s = CGFloat(min(1, max(0, s)))
            let m = 1 - s
            return CGPoint(x: m * m * p.x + 2 * m * s * ctrl.x + s * s * a.x,
                           y: m * m * p.y + 2 * m * s * ctrl.y + s * s * a.y)
        }
        let prog = age / travel
        if prog <= 1 {
            let e = prog < 0.5 ? 2 * prog * prog : 1 - pow(-2 * prog + 2, 2) / 2
            for j in stride(from: 11, through: 0, by: -1) {
                let q = at(e - Double(j) * 0.022)
                let fade = 1 - Double(j) / 12
                let r = head * CGFloat(0.35 + 0.65 * fade)
                ctx.fill(Path(ellipseIn: CGRect(x: q.x - r, y: q.y - r, width: r * 2, height: r * 2)),
                         with: .color(hue.mixed(toWhite: 0.3 * fade).color(0.55 * fade * fade)))
            }
            let q = at(e)
            let gr = head * 3.2
            ctx.fill(Path(ellipseIn: CGRect(x: q.x - gr, y: q.y - gr, width: gr * 2, height: gr * 2)),
                     with: .radialGradient(Gradient(colors: [hue.mixed(toWhite: 0.6).color(0.9), hue.color(0)]),
                                           center: q, startRadius: 0, endRadius: gr))
        } else {
            // Arrival flash at the hub.
            let f = min(1, (age - travel) / (Self.cometLife - travel))
            let rr = (8 + 22 * CGFloat(f)) * u * CGFloat(0.6 + mag / 20)
            ctx.stroke(Path(ellipseIn: CGRect(x: a.x - rr, y: a.y - rr, width: rr * 2, height: rr * 2)),
                       with: .color(hue.color(0.5 * (1 - f))), lineWidth: 1.2 * u)
        }
    }

    private func drawStationTooltip(_ ctx: inout GraphicsContext, _ s: StationVM, at p: CGPoint, g: MapGeometry, size: CGSize) {
        let u = g.u
        let rr = g.coreRadius(s) + 7 * u
        ctx.stroke(Path(ellipseIn: CGRect(x: p.x - rr, y: p.y - rr, width: rr * 2, height: rr * 2)),
                   with: .color(.white.opacity(0.7)), lineWidth: 1 * u)
        let text = Text(NodeLines.detail(s.node))
            .font(.system(size: 11.5 * u, design: .monospaced))
            .foregroundColor(Palette.text)
        let resolved = ctx.resolve(text)
        let ts = resolved.measure(in: CGSize(width: 440 * u, height: 400 * u))
        let pad = 10 * u
        var box = CGRect(x: p.x + rr + 8 * u, y: p.y - ts.height / 2 - pad, width: ts.width + pad * 2, height: ts.height + pad * 2)
        if box.maxX > size.width - 6 { box.origin.x = p.x - rr - 8 * u - box.width }
        box.origin.x = max(6, box.origin.x)
        box.origin.y = min(max(6, box.origin.y), size.height - box.height - 6)
        let shape = Path(roundedRect: box, cornerRadius: 9 * u, style: .continuous)
        ctx.fill(shape, with: .color(Color.black.opacity(0.88)))
        ctx.stroke(shape, with: .color(s.hue.color(0.55)), lineWidth: 1)
        ctx.draw(resolved, in: box.insetBy(dx: pad, dy: pad))
    }

    private func drawTooltip(_ ctx: inout GraphicsContext, _ m: MoonVM, _ s: StationVM, at p: CGPoint, g: MapGeometry, size: CGSize) {
        let u = g.u
        let rr = m.radius * u + 7 * u
        ctx.stroke(Path(ellipseIn: CGRect(x: p.x - rr, y: p.y - rr, width: rr * 2, height: rr * 2)),
                   with: .color(.white.opacity(0.7)), lineWidth: 1 * u)
        var lines = ["\(m.model)  ·  \(s.name)"]
        var mem = m.vram > 0 ? "\(Fmt.one(m.vram)) GB in GPU memory" : "\(Fmt.one(m.gb)) GB on the CPU"
        if m.ctx > 0 { mem += "  ·  ctx \(Fmt.int(m.ctx))" }
        lines.append(mem)
        if m.port > 0 { lines.append("port \(m.port)") }
        for r in m.active {
            lines.append(r.generating ? "generating · \(Fmt.int(r.decoded)) tok" :
                            "prefill · \(Fmt.int(r.promptDone))/\(Fmt.int(r.prompt))")
        }
        if m.active.isEmpty { lines.append("idle") }
        let text = Text(lines.joined(separator: "\n"))
            .font(.system(size: 11.5 * u, design: .monospaced))
            .foregroundColor(Palette.text)
        let resolved = ctx.resolve(text)
        let ts = resolved.measure(in: CGSize(width: 520 * u, height: 400 * u))
        let pad = 10 * u
        var box = CGRect(x: p.x + rr + 8 * u, y: p.y - ts.height / 2 - pad, width: ts.width + pad * 2, height: ts.height + pad * 2)
        if box.maxX > size.width - 6 { box.origin.x = p.x - rr - 8 * u - box.width }
        box.origin.y = min(max(6, box.origin.y), size.height - box.height - 6)
        let shape = Path(roundedRect: box, cornerRadius: 9 * u, style: .continuous)
        ctx.fill(shape, with: .color(Color.black.opacity(0.86)))
        ctx.stroke(shape, with: .color(m.hue.color(0.55)), lineWidth: 1)
        ctx.draw(resolved, in: box.insetBy(dx: pad, dy: pad))
    }
}

// MARK: - Station labels (SwiftUI; change only per poll)

/// Positions a view so its top-center (or bottom-center) sits at a point.
private struct TopAnchored: Layout {
    var point: CGPoint
    var bottom = false
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        proposal.replacingUnspecifiedDimensions()
    }
    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        for v in subviews {
            v.place(at: CGPoint(x: bounds.minX + point.x, y: bounds.minY + point.y), anchor: bottom ? .bottom : .top, proposal: .unspecified)
        }
    }
}

struct StationLabelsLayer: View {
    let stations: [StationVM]
    let size: CGSize
    var body: some View {
        let g = MapGeometry(size: size, stations: stations)
        let u = g.u
        ZStack {
            ForEach(stations) { s in
                if let p = g.points[s.name] {
                    let (a, up) = g.labelAnchor(s)
                    let _ = p
                    TopAnchored(point: a, bottom: up) {
                        StationLabel(s: s, u: u)
                            .frame(width: (s.isHub ? 300 : 240) * u)
                    }
                }
            }
        }
        .frame(width: size.width, height: size.height)
        .allowsHitTesting(false)
    }
}

struct StationLabel: View {
    let s: StationVM
    let u: CGFloat

    private var n: ClusterSnapshot.Node { s.node }
    private var mono: Font { .system(size: 11 * u, design: .monospaced) }

    var body: some View {
        // Always the same lines in the same order (one line each), so a label never changes height and the
        // map never jumps: name, LLM job, purpose, chip · OS, memory, traffic, tonight / models.
        VStack(spacing: 2.5 * u) {
            HStack(spacing: 7 * u) {
                Text(s.name)
                    .font(.system(size: (s.isHub ? 15 : 13.5) * u, weight: .semibold, design: .monospaced))
                    .foregroundStyle(s.hue.mixed(toWhite: 0.15).color())
                    .layoutPriority(2)
                if let alias = NodeLines.alias(n) {
                    Text("(\(alias))")
                        .font(.system(size: 10.5 * u, design: .monospaced))
                        .foregroundStyle(s.hue.mixed(toWhite: 0.15).color(0.6))
                        .layoutPriority(1)
                }
                badge
            }
            .lineLimit(1)
            NodeRoleLines(node: n, u: u, size: 11, alignment: .center)
            Text("\(Fmt.chip(n.chip)) · \(Fmt.os(n.os))")
                .font(mono).foregroundStyle(.white.opacity(0.42)).lineLimit(1)
            Text("\(Fmt.gb(n.free_gb))/\(Fmt.gb(n.mem_gb)) GB free · load \(Fmt.one(n.load))")
                .font(mono).foregroundStyle(.white.opacity(0.42)).lineLimit(1)
            trafficLine
            if let m = n.minion {
                Text(tonight(m.tonight))
                    .font(mono).foregroundStyle(Palette.text.opacity(0.62)).lineLimit(1)
            } else {
                Text(s.moons.isEmpty ? "no models loaded" : s.moons.map { ModelHue.short($0.model) }.joined(separator: " · "))
                    .font(.system(size: 10.5 * u, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.36)).lineLimit(1).truncationMode(.tail)
            }
        }
        .multilineTextAlignment(.center)
        .padding(.horizontal, 8 * u).padding(.vertical, 5 * u)
        .background(RoundedRectangle(cornerRadius: 8 * u).fill(Palette.space.opacity(0.55)).blur(radius: 6 * u))
        .opacity(s.live ? 1 : 0.5)
    }

    @ViewBuilder private var badge: some View {
        if s.isMinion {
            let st = (s.minionState ?? "offline").lowercased()
            Text(st.uppercased())
                .font(.system(size: 9 * u, weight: .semibold)).tracking(1.4 * u)
                .foregroundStyle(Palette.minionState(st).color(0.95))
                .padding(.horizontal, 5 * u).padding(.vertical, 1.5 * u)
                .overlay(Capsule().strokeBorder(Palette.minionState(st).color(0.5), lineWidth: 0.8))
        } else {
            Text(s.serviceLabel.uppercased())
                .font(.system(size: 9 * u, weight: .semibold)).tracking(1.4 * u)
                .foregroundStyle(Palette.cool.opacity(0.85))
        }
    }

    private var trafficLine: some View {
        let tok = n.tok_s_total ?? ((n.tok_s ?? 0) + (n.prefill_tok_s ?? 0)), rpm = n.req_min ?? 0
        let hot = tok > 0 || s.busy > 0
        var parts: [String] = []
        parts.append("\(Fmt.one(tok)) tok/s")
        parts.append("\(Fmt.compact(n.today?.tokens)) today")
        parts.append("\(Fmt.int(rpm))/min")
        return Text(parts.joined(separator: " · "))
            .font(.system(size: 11 * u, weight: hot ? .semibold : .regular, design: .monospaced))
            .foregroundStyle(hot ? Palette.warm : .white.opacity(0.5))
            .lineLimit(1)
    }

    private func tonight(_ t: ClusterSnapshot.Tonight?) -> String {
        let works = Int(t?.works ?? 0), lessons = Int(t?.lessons ?? 0), eps = Int(t?.transcribed ?? 0)
        var parts: [String] = []
        if works > 0 || eps == 0 {
            parts.append(Fmt.plural(works, "work", "works")); parts.append(Fmt.plural(lessons, "lesson", "lessons"))
        }
        if eps > 0 {
            parts.append(Fmt.plural(eps, "episode", "episodes"))
            if works == 0 { parts.append("\(Fmt.one(t?.audio_h)) h") }
        }
        return "tonight: " + parts.joined(separator: " · ")
    }
}
