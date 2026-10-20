//
//  Views.swift
//  Nova Cluster (iPad + Mac)
//
//  Full-screen layout: totals across the top (LLM traffic + the minions' night),
//  the cluster map, a side panel (now generating · recent requests), and along the
//  bottom the latest lesson the traffic produced, plus the model legend and status.
//

import SwiftUI

struct ContentView: View {
    let cluster: ClusterModel
    /// map · dashboard · constellation (Nova Minion, folded in 1.1.0). Tap the wordmark to step through;
    /// Mac: View › Cluster Map (⌘1) / Dashboard (⌘2) / Constellation (⌘3).
    @AppStorage(ViewMode.key) private var modeRaw = ViewMode.map.rawValue
    private var mode: ViewMode { ViewMode(stored: modeRaw) }
    /// Kiosk mode: cycle the three views on a timer; touches pause it (KioskPolicy).
    @AppStorage(KioskPolicy.enabledKey) private var kioskOn = KioskPolicy.defaultEnabled
    @AppStorage(KioskPolicy.intervalKey) private var kioskInterval = KioskPolicy.defaultInterval
    @State private var lastSwitch = Date()
    @State private var lastTouch: Date?
    #if os(macOS)
    @State private var keepAwake: NSObjectProtocol?
    @State private var lastPointer: CGPoint?
    #endif
    @State private var map = MapModel()
    @State private var pointer = MapPointer()
    @State private var version = 0
    @State private var showSettings = false
    @Environment(\.scenePhase) private var phase
    #if os(macOS)
    @Environment(\.openSettings) private var openSettings
    #endif

    private var snap: ClusterSnapshot? { cluster.snapshot }

    private var isLive: Bool {
        #if os(macOS)
        return true
        #else
        return phase == .active
        #endif
    }

    var body: some View {
        ZStack {
            Palette.space.ignoresSafeArea()
            GeometryReader { geo in
                RadialGradient(colors: [Color(red: 0.05, green: 0.06, blue: 0.12).opacity(0.6), .clear],
                               center: .init(x: 0.42, y: 0.46), startRadius: 0, endRadius: geo.size.width * 0.6)
            }
            .ignoresSafeArea()
            DustLayer().ignoresSafeArea()
            switch mode {
            case .dashboard:
                DashboardScreen(cluster: cluster, stations: map.stations, paused: !isLive) { u in mark(u: u) }
                    .transition(.opacity)
            case .constellation:
                ConstellationScreen(cluster: cluster, stations: map.stations, paused: !isLive) { u in mark(u: u) }
                    .transition(.opacity)
            case .map:
                mapScreen.transition(.opacity)
            }
        }
        .animation(.easeInOut(duration: 0.5), value: modeRaw)
        #if os(macOS)
        .onContinuousHover(coordinateSpace: .global) { ph in
            // Hover re-fires as animated content moves under a still cursor: only a real move counts.
            guard case .active(let p) = ph else { return }
            if let q = lastPointer, hypot(p.x - q.x, p.y - q.y) < 3 { return }
            if lastPointer != nil { noteTouch() }
            lastPointer = p
        }
        #elseif os(iOS)
        .simultaneousGesture(DragGesture(minimumDistance: 0).onChanged { _ in noteTouch() })
        #endif
        .onChange(of: modeRaw) { _, _ in lastSwitch = Date() }
        .task(id: kioskOn) { await runKiosk() }
        #if os(macOS)
        .onChange(of: kioskOn, initial: true) { _, on in setKeepAwake(on) }
        #endif
        .preferredColorScheme(.dark)
        #if os(iOS)
        .statusBarHidden(true)
        .persistentSystemOverlays(.hidden)
        .sheet(isPresented: $showSettings) {
            SettingsSheet(cluster: cluster) { showSettings = false }
        }
        .onChange(of: phase) { _, p in
            UIApplication.shared.isIdleTimerDisabled = (p == .active)
            if p == .active { cluster.start() } else { cluster.stop() }
        }
        #endif
        .onAppear { map.update(snap); version &+= 1; cluster.start() }
        .onChange(of: snap) { _, s in map.update(s); version &+= 1 }
        .onOpenURL { url in
            if ClusterConfig.applyView(url: url) { return }
            if KioskPolicy.apply(url: url) { lastSwitch = Date(); return }
            if ClusterConfig.apply(url: url) { cluster.restart() }
        }
    }

    /// Kiosk loop: once a second, step to the next view when the policy says so.
    private func runKiosk() async {
        lastSwitch = Date()
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(1))
            let policy = KioskPolicy(enabled: kioskOn, interval: KioskPolicy.clamp(kioskInterval))
            if policy.shouldAdvance(now: Date(), lastSwitch: lastSwitch, lastTouch: lastTouch) {
                modeRaw = mode.next.rawValue
                lastSwitch = Date()
            }
        }
    }

    /// Any touch / pointer movement pauses the kiosk cycle. Throttled: hover fires constantly.
    private func noteTouch() {
        let now = Date()
        if let t = lastTouch, now.timeIntervalSince(t) < 1 { return }
        lastTouch = now
    }

    #if os(macOS)
    /// In kiosk mode the Mac is a wall display: keep the screen from sleeping.
    private func setKeepAwake(_ on: Bool) {
        if on, keepAwake == nil {
            keepAwake = ProcessInfo.processInfo.beginActivity(options: [.idleDisplaySleepDisabled, .userInitiated],
                                                              reason: "Nova Cluster kiosk mode")
        } else if !on, let a = keepAwake {
            ProcessInfo.processInfo.endActivity(a); keepAwake = nil
        }
    }
    #endif

    private var mapScreen: some View {
        GeometryReader { geo in
            #if os(macOS)
            let u = max(0.8, min(geo.size.width / 1440, geo.size.height / 900))
            let topPad: CGFloat = 30
            #else
            let u = max(0.8, min(geo.size.width, geo.size.height) / 820)
            let topPad: CGFloat = 18
            #endif
            let landscape = geo.size.width > geo.size.height
            VStack(spacing: 0) {
                HeaderBar(snap: snap, ticker: cluster.ticker, paused: !isLive, u: u,
                          widthInU: geo.size.width / u) {
                    mark(u: u)
                }
                .padding(.horizontal, 24 * u)
                .padding(.top, topPad * u)

                if landscape {
                    HStack(alignment: .top, spacing: 0) {
                        mapArea(u: u)
                        SidePanel(snap: snap, u: u)
                            .frame(width: min(370 * u, geo.size.width * 0.3))
                            .padding(.trailing, 20 * u)
                            .padding(.vertical, 14 * u)
                    }
                } else {
                    mapArea(u: u)
                    SidePanel(snap: snap, u: u, compact: true)
                        .frame(height: geo.size.height * 0.30)
                        .padding(.horizontal, 20 * u)
                }

                FooterBar(snap: snap, offline: cluster.isOffline, lastSuccess: cluster.lastSuccess,
                          configured: ClusterConfig.current.isConfigured, u: u)
                    .padding(.horizontal, 24 * u)
                    .padding(.top, 6 * u)
                    .padding(.bottom, 14 * u)
            }
        }
    }

    /// The wordmark: tap switches views, long-press opens settings.
    private func mark(u: CGFloat) -> some View {
        Wordmark(u: u, subtitle: mode.subtitle)
            .onTapGesture { modeRaw = mode.next.rawValue }
            .onLongPressGesture(minimumDuration: 0.8) { presentSettings() }
    }

    private func mapArea(u: CGFloat) -> some View {
        GeometryReader { mg in
            ZStack {
                ClusterMapView(map: map, comets: cluster.comets, pointer: pointer, version: version,
                               dimmed: cluster.isOffline, paused: !isLive)
                StationLabelsLayer(stations: map.stations, size: mg.size)
                    .id(version)
                    .opacity(cluster.isOffline ? 0.6 : 1)
                if map.stations.isEmpty {
                    Text(placeholder)
                        .font(.system(size: 24 * u, weight: .light, design: .serif)).italic()
                        .foregroundStyle(Palette.text.opacity(0.6))
                }
            }
            .contentShape(Rectangle())
            #if os(macOS)
            .onContinuousHover { ph in
                switch ph {
                case .active(let p): pointer.hover = p
                case .ended: pointer.hover = nil
                }
            }
            #endif
            .onTapGesture { loc in pointer.tap = loc }
        }
        .padding(.top, 12 * u)
    }

    private var placeholder: String {
        #if os(macOS)
        if !ClusterConfig.current.isConfigured { return "Open Settings (⌘,) to connect to Nova's hub." }
        #else
        if !ClusterConfig.current.isConfigured { return "Long-press NOVA to connect to Nova's hub." }
        #endif
        return "Listening for Nova's cluster…"
    }

    private func presentSettings() {
        #if os(macOS)
        openSettings()
        #else
        showSettings = true
        #endif
    }
}

// MARK: - Wordmark

struct Wordmark: View {
    let u: CGFloat
    var subtitle = "· CLUSTER"

    var body: some View {
        // A slow 10 fps breath drawn in a fixed-size Canvas, so it never touches layout
        // (a repeatForever animation would redraw the window at display rate).
        TimelineView(.animation(minimumInterval: 0.1)) { tl in
            let t = tl.date.timeIntervalSinceReferenceDate
            Canvas { ctx, size in
                ctx.opacity = 0.71 + 0.21 * sin(t * 2 * .pi / 10)
                ctx.draw(Text("NOVA").font(.system(size: 26 * u, weight: .ultraLight)).tracking(15 * u)
                            .foregroundColor(.white), at: CGPoint(x: size.width, y: 0), anchor: .topTrailing)
                ctx.draw(Text(subtitle).font(.system(size: 10 * u, weight: .medium)).tracking(5 * u)
                            .foregroundColor(Palette.warm), at: CGPoint(x: size.width, y: 37 * u), anchor: .topTrailing)
            }
        }
        .frame(width: 170 * u, height: 52 * u)
        .contentShape(Rectangle())
        .accessibilityElement()
        .accessibilityLabel("Nova Cluster. Tap to switch views, long-press for settings.")
    }
}

// MARK: - Header totals

struct HeaderBar<Mark: View>: View {
    let snap: ClusterSnapshot?
    let ticker: TokenTicker
    let paused: Bool
    let u: CGFloat
    /// Layout by available width in points/u (decided from the window width, not
    /// ViewThatFits — that would re-measure every tile on each hero tick):
    /// ≥ 1300 one row · ≥ 1000 hero + LLM tiles, minion tiles beneath · else stacked.
    var widthInU: CGFloat = 1440
    @ViewBuilder let mark: () -> Mark

    var body: some View {
        HStack(alignment: .top, spacing: 18 * u) {
            if widthInU >= 1300 {
                TokenHero(ticker: ticker, u: u, compact: false, paused: paused)
                divider
                HStack(alignment: .top, spacing: 24 * u) { llm(u); divider; minions(u) }
                Spacer(minLength: 0)
                mark()
            } else if widthInU >= 1000 {
                TokenHero(ticker: ticker, u: u, compact: false, paused: paused)
                divider
                VStack(alignment: .leading, spacing: 10 * u) { llm(u); minions(u) }
                Spacer(minLength: 0)
                mark()
            } else {
                let v = u * 0.86
                VStack(alignment: .leading, spacing: 10 * u) {
                    HStack(alignment: .top) {
                        TokenHero(ticker: ticker, u: u, compact: false, paused: paused)
                        Spacer(minLength: 0)
                        mark()
                    }
                    HStack(alignment: .top, spacing: 18 * v) { llm(v); divider; minions(v) }
                }
            }
        }
    }

    private var divider: some View {
        Rectangle().fill(.white.opacity(0.1)).frame(width: 1, height: 44 * u).padding(.top, 4 * u)
    }

    private func llm(_ u: CGFloat) -> some View {
        let t = snap?.totals
        let nodes = snap?.allNodes ?? []
        let live = nodes.filter(\.isLive).count
        let busy = Int(t?.busy_slots ?? 0)
        return StatGroup(title: "LLM TRAFFIC", u: u) {
            StatTile(value: "\(live)/\(nodes.count)", unit: nil, label: "MACHINES LIVE", u: u,
                     tint: live < nodes.count ? Palette.alert : nil)
            StatTile(value: Fmt.int(t?.models_loaded), unit: nil, label: "MODELS", u: u)
            StatTile(value: Fmt.gb(t?.vram_gb), unit: "GB", label: "MODEL MEMORY", u: u)
            StatTile(value: "\(busy)", unit: nil, label: "ACTIVE", u: u, tint: busy > 0 ? Palette.warm : nil)
            StatTile(value: Fmt.int(t?.req_min), unit: nil, label: "REQ/MIN", u: u)
        }
    }

    private func minions(_ u: CGFloat) -> some View {
        let f = snap?.fleet_tonight
        let eps = Int(f?.transcribed ?? 0)
        let toRead = snap?.queue?["A"]
        return StatGroup(title: "MINIONS TONIGHT", u: u) {
            StatTile(value: Fmt.int(f?.works), unit: nil, label: "WORKS", u: u, tint: Palette.terminal)
            StatTile(value: Fmt.int(f?.lessons), unit: nil, label: "LESSONS", u: u, tint: Palette.terminal)
            StatTile(value: "\(eps)", unit: eps > 0 ? "· \(Fmt.one(f?.audio_h)) h" : nil, label: "EPISODES", u: u)
            StatTile(value: Fmt.int(toRead), unit: snap?.eta_days.map { "~\(Int($0.rounded())) d" }, label: "TO READ", u: u)
        }
    }
}

/// The hero: tokens the whole cluster has processed today, rolling like an odometer
/// (extrapolated between polls at the cluster's live tok/s), with the rate beside it
/// and the all-time total under it. Drawn at 15 fps in a fixed-size Canvas so its
/// ticking never re-lays out the window.
struct TokenHero: View {
    let ticker: TokenTicker
    let u: CGFloat
    var compact: Bool
    var paused: Bool

    private var width: CGFloat { (compact ? 250 : 280) * u }
    private var height: CGFloat { (compact ? 66 : 76) * u }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 15.0, paused: paused)) { tl in
            Canvas { ctx, size in draw(&ctx, now: tl.date) }
        }
        .frame(width: width, height: height)
        .background(
            RoundedRectangle(cornerRadius: 30 * u).fill(Palette.space.opacity(0.7))
                .padding(-14 * u).blur(radius: 14 * u)
        )
        .accessibilityElement()
        .accessibilityLabel("Tokens today")
    }

    private func draw(_ ctx: inout GraphicsContext, now: Date) {
        let today = ticker.hasData ? ticker.today(at: now) : 0
        let rate = ticker.currentRate
        let size: CGFloat = (compact ? 30 : 36) * u
        let h = size * 1.18
        let top: CGFloat = 13 * u
        ctx.draw(Text("TOKENS TODAY").font(.system(size: 9 * u, weight: .semibold)).tracking(2.4 * u)
                    .foregroundColor(Palette.warm.opacity(0.7)), at: .zero, anchor: .topLeading)

        let font = Font.system(size: size, weight: .ultraLight).monospacedDigit()
        let digits = (0...9).map { ctx.resolve(Text("\($0)").font(font).foregroundColor(Palette.text)) }
        let comma = ctx.resolve(Text(",").font(font).foregroundColor(Palette.text.opacity(0.6)))
        let dw = digits[8].measure(in: CGSize(width: 200, height: 200)).width
        let cw = comma.measure(in: CGSize(width: 200, height: 200)).width
        let v = max(0, today)
        let n = String(Int(v)).count
        var x: CGFloat = 0
        for k in 0..<n {
            let place = n - 1 - k
            let r = Odometer.roll(v, place, rate: rate)
            let d = Int(r) % 10
            let f = CGFloat(r - r.rounded(.down))
            var c = ctx
            c.clip(to: Path(CGRect(x: x, y: top, width: dw, height: h)))
            c.draw(digits[d], at: CGPoint(x: x, y: top - f * h), anchor: .topLeading)
            if f > 0 { c.draw(digits[(d + 1) % 10], at: CGPoint(x: x, y: top + h - f * h), anchor: .topLeading) }
            x += dw
            if place > 0 && place % 3 == 0 {
                ctx.draw(comma, at: CGPoint(x: x, y: top), anchor: .topLeading)
                x += cw
            }
        }
        let rateText = Text(Fmt.one(rate)).font(.system(size: 16 * u).monospacedDigit())
            .foregroundColor(rate > 0 ? Palette.warm : .white.opacity(0.5))
            + Text(" tok/s").font(.system(size: 10.5 * u)).foregroundColor(.white.opacity(0.45))
        ctx.draw(rateText, at: CGPoint(x: x + 10 * u, y: top + h * 0.80), anchor: .bottomLeading)
        ctx.draw(Text("all time \(Fmt.int(ticker.allTime(at: now)))")
                    .font(.system(size: 10.5 * u, design: .monospaced)).foregroundColor(.white.opacity(0.4)),
                 at: CGPoint(x: 0, y: top + h + 2 * u), anchor: .topLeading)
    }
}

/// Odometer roll maths (drawn by TokenHero): a column turns over in a quick ~0.25 s
/// roll as the value passes it; a column that changes more than ~5× a second just
/// snaps (a blur of spinning digits is unreadable).
enum Odometer {
    /// Position (0 ..< 10, fractional while rolling) of the digit at 10^place.
    static func roll(_ v: Double, _ place: Int, rate: Double) -> Double {
        let p = pow(10, Double(place))
        let q = (v / p).rounded(.down)
        let d = q.truncatingRemainder(dividingBy: 10)
        if rate / p > 5 { return d }
        let below = v - q * p                                  // 0 ..< p
        let w = max(min(0.9 * p, rate * 0.25), 1e-6)            // roll spans the last w units
        return d + max(0, min(1, (below - (p - w)) / w)) * 0.999
    }
}

struct StatGroup<Content: View>: View {
    let title: String
    let u: CGFloat
    @ViewBuilder let content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 5 * u) {
            Text(title)
                .font(.system(size: 9 * u, weight: .semibold)).tracking(2.4 * u)
                .foregroundStyle(.white.opacity(0.3))
            HStack(alignment: .firstTextBaseline, spacing: 20 * u) { content() }
        }
        .fixedSize()
    }
}

struct StatTile: View {
    let value: String
    let unit: String?
    let label: String
    let u: CGFloat
    var tint: Color?

    var body: some View {
        VStack(alignment: .leading, spacing: 2 * u) {
            HStack(alignment: .firstTextBaseline, spacing: 4 * u) {
                Text(value)
                    .font(.system(size: 22 * u, weight: .light).monospacedDigit())
                    .foregroundStyle(tint ?? Palette.text)
                if let unit {
                    Text(unit).font(.system(size: 11 * u, weight: .regular)).foregroundStyle(.white.opacity(0.45))
                }
            }
            Text(label)
                .font(.system(size: 8.5 * u, weight: .medium)).tracking(1.5 * u)
                .foregroundStyle(.white.opacity(0.4))
        }
        .lineLimit(1)
        .fixedSize()
    }
}

// MARK: - Side panel

struct SidePanel: View {
    let snap: ClusterSnapshot?
    let u: CGFloat
    var compact = false

    private var active: [ActiveRequest] { (snap?.allNodes ?? []).flatMap(\.activeRequests) }
    private var events: [ClusterSnapshot.Event] { Array((snap?.allEvents ?? []).prefix(60)) }

    var body: some View {
        Group {
            if compact {
                HStack(alignment: .top, spacing: 14 * u) { nowGenerating.frame(maxWidth: .infinity); recent.frame(maxWidth: .infinity) }
            } else {
                VStack(alignment: .leading, spacing: 14 * u) { nowGenerating; recent }
            }
        }
    }

    private func card<C: View>(_ c: C) -> some View {
        c
            .padding(.horizontal, 14 * u).padding(.vertical, 12 * u)
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .background(
                RoundedRectangle(cornerRadius: 14 * u, style: .continuous)
                    .fill(Color.black.opacity(0.62))
                    .overlay(RoundedRectangle(cornerRadius: 14 * u, style: .continuous)
                        .strokeBorder(Color.white.opacity(0.08), lineWidth: 1))
            )
    }

    private func heading(_ title: String, _ count: String?, tint: Color) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title).font(.system(size: 10 * u, weight: .semibold)).tracking(2.6 * u).foregroundStyle(tint)
            Spacer()
            if let count {
                Text(count).font(.system(size: 11 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.4))
            }
        }
    }

    private var nowGenerating: some View {
        let list = active
        return card(VStack(alignment: .leading, spacing: 8 * u) {
            heading("NOW GENERATING", list.isEmpty ? nil : "\(list.count) in flight", tint: Palette.warm)
            if list.isEmpty {
                Text("nothing in flight — between requests")
                    .font(.system(size: 12 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.32))
            } else {
                ForEach(list.prefix(compact ? 4 : 7)) { r in ActiveRow(r: r, u: u) }
                if list.count > (compact ? 4 : 7) {
                    Text("+ \(list.count - (compact ? 4 : 7)) more")
                        .font(.system(size: 11 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.35))
                }
            }
        })
        .animation(.easeOut(duration: 0.4), value: list.map(\.id))
    }

    private var recent: some View {
        let list = events
        let now = Date()
        return card(VStack(alignment: .leading, spacing: 6 * u) {
            heading("RECENT REQUESTS", snap?.totals?.req_min.map { "\(Fmt.int($0))/min" }, tint: Palette.cool)
            if list.isEmpty {
                Text("no finished requests yet")
                    .font(.system(size: 12 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.32))
            }
            // Overlay so the list never sizes the panel: it fills what's left and clips.
            Color.clear
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .overlay(alignment: .top) {
                    VStack(alignment: .leading, spacing: 5 * u) {
                        ForEach(Array(list.enumerated()), id: \.element.key) { i, e in
                            EventRow(e: e, now: now, u: u)
                                .opacity(max(0.25, 1 - Double(i) * 0.03))
                                .transition(.asymmetric(insertion: .move(edge: .top).combined(with: .opacity), removal: .opacity))
                        }
                    }
                }
                .clipped()
            .animation(.easeOut(duration: 0.5), value: list.first?.key)
        }
        .frame(maxHeight: .infinity, alignment: .top))
    }
}

struct ActiveRow: View {
    let r: ActiveRequest
    let u: CGFloat
    var body: some View {
        let hue = ModelHue.of(r.model)
        VStack(alignment: .leading, spacing: 4 * u) {
            HStack(spacing: 6 * u) {
                Circle().fill(hue.color()).frame(width: 7 * u, height: 7 * u).shadow(color: hue.color(0.9), radius: 3 * u)
                Text(ModelHue.short(r.model)).font(.system(size: 12.5 * u, weight: .semibold, design: .monospaced))
                    .foregroundStyle(hue.color()).truncationMode(.tail)
                Text("· \(r.node)").font(.system(size: 12 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.55))
                    .truncationMode(.tail)
                Spacer(minLength: 4 * u)
                Text(r.generating ? "generating \(Fmt.int(r.decoded)) tok" : "prefill \(Fmt.int(r.promptDone))/\(Fmt.int(r.prompt))")
                    .font(.system(size: 11.5 * u, design: .monospaced))
                    .foregroundStyle(r.generating ? Palette.terminal : Palette.warm)
                    .fixedSize()
                    .layoutPriority(1)
            }
            .lineLimit(1)
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(.white.opacity(0.08))
                    Capsule().fill((r.generating ? hue : Palette.amber).color(0.8))
                        .frame(width: g.size.width * (r.generating ? 1 : max(0.02, r.prefillFraction)))
                }
            }
            .frame(height: 2.5 * u)
            .opacity(r.generating ? 0.35 : 1)
        }
    }
}

struct EventRow: View {
    let e: ClusterSnapshot.Event
    let now: Date
    let u: CGFloat
    var body: some View {
        let model = e.model ?? "?"
        let hue = ModelHue.of(model)
        let ago = Fmt.date(e.at).map { Fmt.ago(now.timeIntervalSince($0)) } ?? ""
        HStack(spacing: 6 * u) {
            Circle().fill(hue.color(0.9)).frame(width: 5 * u, height: 5 * u)
            Text(ModelHue.short(model)).foregroundStyle(hue.color(0.95))
            Text("· \(e.node ?? "")").foregroundStyle(.white.opacity(0.5))
            Spacer(minLength: 4 * u)
            Text("\(Fmt.int(e.prompt))→\(Fmt.int(e.decoded))").foregroundStyle(Palette.text.opacity(0.7))
            Text(ago).foregroundStyle(.white.opacity(0.35)).frame(width: 30 * u, alignment: .trailing)
        }
        .font(.system(size: 11.5 * u, design: .monospaced))
        .lineLimit(1)
    }
}

// MARK: - Footer: latest lesson, legend, status

struct FooterBar: View {
    let snap: ClusterSnapshot?
    let offline: Bool
    let lastSuccess: Date?
    let configured: Bool
    let u: CGFloat

    var body: some View {
        VStack(spacing: 8 * u) {
            LessonCaption(lessons: snap?.lessons ?? [], u: u)
            HStack(spacing: 16 * u) {
                legend
                Spacer(minLength: 10 * u)
                status
            }
        }
    }

    private var families: [String] {
        var seen: [String] = []
        for n in snap?.allNodes ?? [] {
            for l in n.loaded ?? [] {
                let f = ModelHue.family(l.name)
                if !seen.contains(f) { seen.append(f) }
            }
        }
        return seen
    }

    private var legend: some View {
        HStack(spacing: 13 * u) {
            ForEach(families, id: \.self) { f in
                HStack(spacing: 5 * u) {
                    Circle().fill(ModelHue.of(f).color()).frame(width: 7 * u, height: 7 * u)
                        .shadow(color: ModelHue.of(f).color(0.9), radius: 3 * u)
                    Text(f).foregroundStyle(.white.opacity(0.5))
                }
            }
            Text("◆ service node · ring = minion state · hollow = CPU")
                .foregroundStyle(.white.opacity(0.3))
        }
        .font(.system(size: 11 * u, design: .monospaced))
        .lineLimit(1)
        .minimumScaleFactor(0.7)
    }

    private var status: some View {
        Group {
            if !configured {
                Text("not connected").foregroundStyle(Palette.warm.opacity(0.85))
            } else if offline {
                Text("hub unreachable" + (lastSuccess.map { " · last heard \(Fmt.clock($0))" } ?? "") + " · showing the last map")
                    .foregroundStyle(Palette.warm.opacity(0.9))
            } else {
                HStack(spacing: 6 * u) {
                    Circle().fill(Color(red: 0.42, green: 0.95, blue: 0.62)).frame(width: 6 * u, height: 6 * u)
                    Text("live · \(Fmt.clock(lastSuccess))").foregroundStyle(.white.opacity(0.45))
                }
            }
        }
        .font(.system(size: 12 * u))
        .lineLimit(1)
        .fixedSize()
    }
}

/// The latest lesson the minions learned — what all that traffic is for. Crossfades
/// to a brand-new lesson at once, otherwise steps back through recent ones every ~20 s.
struct LessonCaption: View {
    let lessons: [ClusterSnapshot.Lesson]
    let u: CGFloat
    @State private var shownKey: String?
    @State private var visible = 0.0
    @State private var lastNewest: String?
    @State private var lastSwitch = Date()

    var body: some View {
        let shown = lessons.first { $0.key == shownKey }
        VStack(spacing: 3 * u) {
            if let l = shown {
                Text("\u{201C}\(LessonText.clean(l.lesson ?? ""))\u{201D}")
                    .font(.system(size: 16 * u, weight: .light, design: .serif)).italic()
                    .foregroundStyle(Palette.text.opacity(0.82))
                    .lineLimit(1).truncationMode(.tail)
                    .minimumScaleFactor(0.8)
                Text(meta(l))
                    .font(.system(size: 9.5 * u, weight: .medium)).tracking(1.8 * u)
                    .foregroundStyle(.white.opacity(0.32))
                    .lineLimit(1).truncationMode(.middle)
            } else {
                Text(" ").font(.system(size: 16 * u))
                Text(" ").font(.system(size: 9.5 * u))
            }
        }
        .opacity(visible)
        .frame(maxWidth: .infinity)
        .onAppear { advance(force: true) }
        .onChange(of: lessons.first?.key) { _, _ in advance(force: false) }
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                if Date().timeIntervalSince(lastSwitch) >= 20 { step() }
            }
        }
    }

    private func meta(_ l: ClusterSnapshot.Lesson) -> String {
        var parts = ["LATEST LESSON"]
        parts.append(l.node.map { "LEARNED ON \($0.uppercased())" } ?? "LEARNED ON NOVA'S HUB")
        if let t = l.title { parts.append("FROM \(t.uppercased())") }
        if let d = Fmt.date(l.at) { parts.append(Fmt.clock(d).uppercased()) }
        return parts.joined(separator: "  ·  ")
    }

    private func advance(force: Bool) {
        guard let newest = lessons.first?.key else { return }
        if force || newest != lastNewest {
            lastNewest = newest
            swap(to: newest)
        }
    }

    private func step() {
        let pool = Array(lessons.prefix(20))
        guard pool.count > 1 else { lastSwitch = Date(); return }
        let i = (pool.firstIndex { $0.key == shownKey } ?? -1) + 1
        swap(to: pool[i % pool.count].key)
    }

    private func swap(to key: String) {
        lastSwitch = Date()
        guard key != shownKey else { return }
        Task { @MainActor in
            if shownKey != nil {
                withAnimation(.easeIn(duration: 0.8)) { visible = 0 }
                try? await Task.sleep(for: .seconds(0.85))
            }
            shownKey = key
            withAnimation(.easeOut(duration: 1.4)) { visible = 1 }
        }
    }
}

// MARK: - Settings (iPad)

#if os(iOS)
struct SettingsSheet: View {
    let cluster: ClusterModel
    let done: () -> Void
    @State private var draft = ClusterConfig.current

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("http://hub-host:port", text: $draft.hub)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    TextField("node", text: $draft.node)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    SecureField("token", text: $draft.token)
                } header: { Text("Nova's hub") } footer: {
                    Text("The token is stored in this iPad's Keychain. Also configurable with novacluster://setup?hub=…&node=…&token=…")
                }
                Section("Status") {
                    LabeledContent("Connection", value: cluster.isOffline ? "unreachable (\(cluster.lastError ?? "no reply"))" : "live")
                    LabeledContent("Last heard", value: cluster.lastSuccess.map { Fmt.clock($0) } ?? "never")
                    LabeledContent("Machines", value: "\(cluster.snapshot?.allNodes.count ?? 0)")
                }
                KioskSettingsSection()
            }
            .navigationTitle("Nova Cluster")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel", action: done) }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { draft.save(); cluster.restart(); done() }
                }
            }
        }
        .preferredColorScheme(.dark)
    }
}
#endif


/// Kiosk controls (Mac Settings and the iPad sheet). They apply at once; no Save needed.
struct KioskSettingsSection: View {
    @AppStorage(KioskPolicy.enabledKey) private var on = KioskPolicy.defaultEnabled
    @AppStorage(KioskPolicy.intervalKey) private var interval = KioskPolicy.defaultInterval

    var body: some View {
        Section {
            Toggle("Kiosk mode", isOn: $on)
            Stepper(value: $interval, in: 10...600, step: 10) {
                LabeledContent("Each view", value: "\(Int(KioskPolicy.clamp(interval))) s")
            }
            .disabled(!on)
        } header: { Text("Kiosk") } footer: {
            Text("Cycles Cluster Map → Dashboard → Constellation. A touch pauses it for two minutes. Also novacluster://kiosk?on=1&interval=60.")
        }
    }
}
