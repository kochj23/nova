//
//  Views.swift
//  Nova Minion (iPad)
//
//  Full-screen display: drifting constellation, minion cards across the top,
//  the current lesson crossfading in serif, fleet totals along the bottom.
//

import SwiftUI

// MARK: - Formatting helpers

enum Fmt {
    nonisolated(unsafe) private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime]; return f
    }()
    nonisolated(unsafe) private static let isoFrac: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f
    }()
    static func date(_ s: String?) -> Date? {
        guard let s else { return nil }
        return iso.date(from: s) ?? isoFrac.date(from: s)
    }
    static func clock(_ d: Date?) -> String {
        guard let d else { return "" }
        return d.formatted(date: .omitted, time: .shortened)
    }
    static func int(_ n: Int) -> String { n.formatted(.number.grouping(.automatic)) }
    static func plural(_ n: Int, _ one: String, _ many: String) -> String { "\(int(n)) \(n == 1 ? one : many)" }
}

// MARK: - Root

struct ContentView: View {
    @State private var fleet = FleetModel()
    @State private var stars = StarModel()
    @State private var starVersion = 0
    @State private var currentKey: String?
    @State private var showSettings = false
    @State private var config = MinionConfig.current
    @Environment(\.scenePhase) private var phase

    private var snap: FleetSnapshot? { fleet.snapshot }
    private var colors: NodeColors { NodeColors(snapshot: snap) }

    var body: some View {
        GeometryReader { geo in
            let u = max(0.8, min(geo.size.width, geo.size.height) / 820)
            let landscape = geo.size.width > geo.size.height
            ZStack {
                Palette.space.ignoresSafeArea()
                RadialGradient(colors: [Color(red: 0.05, green: 0.06, blue: 0.12).opacity(0.55), .clear],
                               center: .init(x: 0.5, y: 0.42), startRadius: 0, endRadius: geo.size.width * 0.7)
                    .ignoresSafeArea()
                DustLayer().ignoresSafeArea()
                StarfieldView(model: stars, version: starVersion, highlightKey: currentKey,
                              paused: phase != .active)
                    .ignoresSafeArea()

                VStack(spacing: 0) {
                    let strip = MinionStrip(minions: snap?.fleet ?? [], colors: colors, u: u, landscape: landscape)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    let mark = Wordmark(u: u)
                        .onLongPressGesture(minimumDuration: 0.8) { showSettings = true }
                    Group {
                        if landscape {
                            HStack(alignment: .top, spacing: 20 * u) { strip; mark }
                        } else {
                            VStack(alignment: .trailing, spacing: 14 * u) { mark; strip }
                        }
                    }
                    .padding(.horizontal, 26 * u)
                    .padding(.top, 22 * u)

                    Spacer(minLength: 10 * u)

                    LessonPanel(lesson: currentLesson, colors: colors, u: u,
                                width: geo.size.width * (landscape ? 0.74 : 0.86),
                                placeholder: placeholder)
                        .padding(.bottom, (landscape ? 30 : 70) * u)

                    BottomBar(snap: snap, colors: colors, offline: fleet.isOffline,
                              lastSuccess: fleet.lastSuccess, u: u)
                        .padding(.horizontal, 26 * u)
                        .padding(.bottom, 18 * u)
                }
            }
        }
        .preferredColorScheme(.dark)
        .statusBarHidden(true)
        .persistentSystemOverlays(.hidden)
        .sheet(isPresented: $showSettings) {
            SettingsSheet(config: $config, fleet: fleet) { showSettings = false }
        }
        .onAppear { rebuildStars(); fleet.start() }
        .onChange(of: snap) { _, _ in rebuildStars() }
        .onChange(of: phase) { _, p in
            UIApplication.shared.isIdleTimerDisabled = (p == .active)
            if p == .active { fleet.start() } else { fleet.stop() }
        }
        .onOpenURL { url in
            if MinionConfig.apply(url: url) {
                config = .current
                fleet.restart()
            }
        }
        .task { await rotateLessons() }
    }

    private var placeholder: String {
        if !MinionConfig.current.isConfigured { return "Long-press NOVA to connect to Nova's hub." }
        return fleet.isOffline && snap == nil ? "Waiting to hear from Nova…" : "Listening for Nova…"
    }

    private var currentLesson: FleetSnapshot.Lesson? {
        guard let lessons = snap?.lessons, !lessons.isEmpty else { return nil }
        return lessons.first { $0.key == currentKey } ?? lessons.first
    }

    private func rebuildStars() {
        stars.update(lessons: snap?.lessons ?? [], colors: colors)
        starVersion &+= 1
        // A brand-new lesson takes the stage immediately.
        if let newest = snap?.lessons.first?.key, newest != lastNewest {
            lastNewest = newest
            currentKey = newest
            lastSwitch = Date()
        }
    }

    @State private var lastNewest: String?
    @State private var lastSwitch = Date()

    /// Every ~22 s, step back through the recent lessons so the screen never goes stale.
    private func rotateLessons() async {
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(2))
            guard phase == .active, Date().timeIntervalSince(lastSwitch) >= 22,
                  let lessons = snap?.lessons, lessons.count > 1 else { continue }
            let pool = Array(lessons.prefix(24))
            let idx = (pool.firstIndex { $0.key == currentKey } ?? -1) + 1
            currentKey = pool[idx % pool.count].key
            lastSwitch = Date()
        }
    }
}

// MARK: - Wordmark

struct Wordmark: View {
    let u: CGFloat
    @State private var breathe = false

    var body: some View {
        VStack(alignment: .trailing, spacing: 3 * u) {
            Text("NOVA")
                .font(.system(size: 26 * u, weight: .ultraLight))
                .tracking(15 * u)
                .foregroundStyle(.white)
            Text("MINION")
                .font(.system(size: 10 * u, weight: .medium))
                .tracking(5 * u)
                .foregroundStyle(Palette.warm)
        }
        .opacity(breathe ? 0.92 : 0.5)
        .contentShape(Rectangle())
        .fixedSize()
        .onAppear {
            withAnimation(.easeInOut(duration: 5).repeatForever(autoreverses: true)) { breathe = true }
        }
        .accessibilityLabel("Nova Minion. Long-press for settings.")
    }
}

// MARK: - Minion cards

struct MinionStrip: View {
    let minions: [FleetSnapshot.Minion]
    let colors: NodeColors
    let u: CGFloat
    let landscape: Bool

    var body: some View {
        if minions.isEmpty {
            HStack {
                Text("no minions reporting")
                    .font(.system(size: 14 * u, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.35))
                Spacer()
            }
        } else {
            let shown = Array(minions.prefix(6))
            let n = min(shown.count, landscape ? 3 : 2)
            if n == 1 {
                HStack(spacing: 0) {
                    MinionCard(minion: shown[0], hue: colors.hue(for: shown[0].node), u: u)
                        .frame(maxWidth: 470 * u)
                    Spacer(minLength: 0)
                }
            } else {
                let cols = Array(repeating: GridItem(.flexible(), spacing: 14 * u, alignment: .top), count: n)
                LazyVGrid(columns: cols, alignment: .leading, spacing: 14 * u) {
                    ForEach(shown) { m in
                        MinionCard(minion: m, hue: colors.hue(for: m.node), u: u)
                    }
                }
            }
        }
    }
}

struct MinionCard: View {
    let minion: FleetSnapshot.Minion
    let hue: Palette.RGB
    let u: CGFloat

    private var state: String { (minion.state ?? "offline").lowercased() }
    private var stateColor: Color {
        switch state {
        case "dedicated": return Color(red: 0.42, green: 0.95, blue: 0.62)
        case "idle": return Color(red: 1.0, green: 0.78, blue: 0.36)
        case "away": return Color(red: 0.55, green: 0.66, blue: 0.90)
        default: return Color(red: 0.95, green: 0.38, blue: 0.40)
        }
    }

    var body: some View {
        let mono = Font.system(size: 14 * u, weight: .regular, design: .monospaced)
        VStack(alignment: .leading, spacing: 5 * u) {
            HStack(alignment: .firstTextBaseline) {
                Circle().fill(stateColor).frame(width: 9 * u, height: 9 * u)
                    .shadow(color: stateColor.opacity(0.8), radius: 4 * u)
                    .alignmentGuide(.firstTextBaseline) { d in d[.bottom] - 1 * u }
                Text(minion.node)
                    .font(.system(size: 17 * u, weight: .semibold, design: .monospaced))
                    .foregroundStyle(hue.color())
                    .lineLimit(1)
                Spacer(minLength: 8 * u)
                if let t = minion.tok_s, state != "offline" {
                    Text(String(format: "%.0f tok/s", t))
                        .font(mono).foregroundStyle(.white.opacity(0.55))
                        .lineLimit(1)
                        .contentTransition(.numericText())
                }
            }
            Text(stateLine)
                .font(.system(size: 12 * u, weight: .medium))
                .tracking(1.6 * u)
                .foregroundStyle(stateColor.opacity(0.8))
                .lineLimit(1)
            if let os = minion.os, !os.isEmpty {
                Text(os)
                    .font(.system(size: 12 * u, weight: .regular, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.45))
                    .lineLimit(1)
            }
            Text("tonight: \(Fmt.plural(minion.tonight?.works ?? 0, "work", "works")) · \(Fmt.plural(minion.tonight?.lessons ?? 0, "lesson", "lessons"))")
                .font(mono).foregroundStyle(Palette.text.opacity(0.78))
                .lineLimit(1)

            let reading = minion.now_reading ?? []
            VStack(alignment: .leading, spacing: 3 * u) {
                if reading.isEmpty {
                    Text(state == "offline" ? "  —" : "  waiting for work…")
                        .font(mono).foregroundStyle(.white.opacity(0.3))
                } else {
                    ForEach(Array(reading.prefix(3).enumerated()), id: \.offset) { i, r in
                        TypedLine(text: readingText(r), first: i == 0, u: u)
                    }
                    if reading.count > 3 {
                        Text("  + \(reading.count - 3) more")
                            .font(mono).foregroundStyle(.white.opacity(0.32))
                    }
                }
            }
            .frame(minHeight: 3 * 19 * u, alignment: .topLeading)
            .padding(.top, 3 * u)
        }
        .padding(.horizontal, 16 * u)
        .padding(.vertical, 13 * u)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 14 * u, style: .continuous)
                .fill(Color.black.opacity(0.62))
                .overlay(alignment: .leading) {
                    // Thin node-hue edge ties the card to its stars.
                    RoundedRectangle(cornerRadius: 2, style: .continuous)
                        .fill(hue.color(0.75))
                        .frame(width: 3 * u)
                        .padding(.vertical, 14 * u)
                }
                .overlay(RoundedRectangle(cornerRadius: 14 * u, style: .continuous)
                    .strokeBorder(Color.white.opacity(0.08), lineWidth: 1))
        )
        .opacity(state == "offline" ? 0.6 : 1)
    }

    private var stateLine: String {
        var s = state.uppercased()
        if let d = Fmt.date(minion.since) { s += "  ·  SINCE \(Fmt.clock(d).uppercased())" }
        return s
    }

    private func readingText(_ r: FleetSnapshot.Reading) -> String {
        let p = (r.phase ?? "").lowercased()
        if p == "transcribing" { return "transcribing: \(r.title)" }     // a minion's Whisper job (TV)
        var s = "reading: \(r.title)"
        if let k = r.kind, !k.isEmpty { s += " (\(k))" }
        if !p.isEmpty { s += " · \(p)…" }
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
        let mono = Font.system(size: 14 * u, weight: .regular, design: .monospaced)
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
        .font(mono)
        .lineLimit(1)
        .truncationMode(.tail)
        .task(id: text) {
            shown = 0
            let chars = text.count
            for i in stride(from: 0, through: chars, by: 1) {
                shown = i
                try? await Task.sleep(for: .milliseconds(28))
                if Task.isCancelled { return }
            }
        }
    }
}

// MARK: - Lesson

struct LessonPanel: View {
    let lesson: FleetSnapshot.Lesson?
    let colors: NodeColors
    let u: CGFloat
    let width: CGFloat
    let placeholder: String
    @State private var shown: FleetSnapshot.Lesson?
    @State private var visible = 0.0

    var body: some View {
        ZStack {
            if let l = shown {
                LessonBody(lesson: l, hue: colors.hue(for: l.node), u: u)
                    .opacity(visible)
                    .offset(y: (1 - visible) * 8 * u)
            } else {
                Text(placeholder)
                    .font(.system(size: 30 * u, weight: .light, design: .serif))
                    .italic()
                    .foregroundStyle(Palette.text.opacity(0.6))
            }
        }
        .frame(width: width)
        .frame(minHeight: 230 * u, alignment: .bottom)
        .background(
            // Soft dark pool so the constellation never fights the quote.
            Rectangle()
                .fill(RadialGradient(colors: [Palette.space.opacity(0.85), Palette.space.opacity(0.5), Palette.space.opacity(0)],
                                     center: .center, startRadius: 0, endRadius: width * 0.62))
                .frame(width: width * 1.24, height: width * 1.24)
                .scaleEffect(x: 1, y: 0.4)
                .allowsHitTesting(false)
        )
        .task(id: lesson?.key) {
            // Fade the old lesson out fully, then bring the new one in (no muddled overlap).
            if shown != nil, shown?.key != lesson?.key {
                withAnimation(.easeIn(duration: 1.1)) { visible = 0 }
                try? await Task.sleep(for: .seconds(1.15))
                if Task.isCancelled { return }
            }
            shown = lesson
            withAnimation(.easeOut(duration: 1.8)) { visible = 1 }
        }
    }
}

struct LessonBody: View {
    let lesson: FleetSnapshot.Lesson
    let hue: Palette.RGB
    let u: CGFloat

    var body: some View {
        VStack(spacing: 14 * u) {
            Text("\u{201C}\(LessonText.clean(lesson.lesson))\u{201D}")
                .font(.system(size: 38 * u, weight: .light, design: .serif))
                .foregroundStyle(Palette.text)
                .multilineTextAlignment(.center)
                .lineSpacing(6 * u)
                .lineLimit(5)
                .minimumScaleFactor(0.62)
                .shadow(color: .black.opacity(0.9), radius: 10 * u)
            Text("— from \(lesson.title)")
                .font(.system(size: 19 * u, weight: .light))
                .italic()
                .foregroundStyle(hue.color(0.95))
                .lineLimit(1)
                .truncationMode(.middle)
                .shadow(color: .black.opacity(0.9), radius: 6 * u)
            Text(meta)
                .font(.system(size: 11 * u, weight: .medium))
                .tracking(2.2 * u)
                .foregroundStyle(.white.opacity(0.38))
        }
    }

    private var meta: String {
        var parts: [String] = []
        if let k = lesson.kind, !k.isEmpty { parts.append(k.uppercased()) }
        parts.append(lesson.node.map { "LEARNED ON \($0.uppercased())" } ?? "LEARNED ON NOVA'S HUB")
        if let d = Fmt.date(lesson.at) { parts.append(Fmt.clock(d).uppercased()) }
        return parts.joined(separator: "  ·  ")
    }
}

// MARK: - Bottom bar

struct BottomBar: View {
    let snap: FleetSnapshot?
    let colors: NodeColors
    let offline: Bool
    let lastSuccess: Date?
    let u: CGFloat

    var body: some View {
        VStack(spacing: 10 * u) {
            if colors.order.count > 0 || snap?.lessons.contains(where: { $0.node == nil }) == true {
                legend
            }
            statusLine
        }
    }

    private var legend: some View {
        HStack(spacing: 18 * u) {
            ForEach(colors.order, id: \.self) { n in item(n, colors.hue(for: n)) }
            if snap?.lessons.contains(where: { $0.node == nil }) == true { item("hub", Palette.hubHue) }
        }
    }

    private func item(_ name: String, _ hue: Palette.RGB) -> some View {
        HStack(spacing: 6 * u) {
            Circle().fill(hue.color()).frame(width: 7 * u, height: 7 * u)
                .shadow(color: hue.color(0.9), radius: 3 * u)
            Text(name).font(.system(size: 12 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.5))
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
            let ft = s.fleet_tonight
            let m = ft?.minions ?? s.fleet.count
            line = line + Text("fleet: \(Fmt.plural(m, "minion", "minions"))").foregroundStyle(Palette.warm.opacity(0.9))
                + sep + Text("\(Fmt.plural(ft?.works ?? 0, "work", "works"))")
                + sep + Text("\(Fmt.plural(ft?.lessons ?? 0, "lesson", "lessons")) tonight")
            if let a = s.queue?["A"] {
                var still = "\(Fmt.int(a)) still to read"
                if let e = s.eta_days { still += " (~\(Int(e.rounded())) days)" }
                line = line + sep + Text(still)
            }
        }
        return line
            .font(.system(size: 14 * u, weight: .regular))
            .foregroundStyle(.white.opacity(0.52))
            .lineLimit(1)
            .minimumScaleFactor(0.7)
    }
}

// MARK: - Settings

struct SettingsSheet: View {
    @Binding var config: MinionConfig
    let fleet: FleetModel
    let done: () -> Void
    @State private var draft = MinionConfig.current

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("http://hub-host:port", text: $draft.hub)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    TextField("node", text: $draft.node)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    SecureField("minion token", text: $draft.token)
                } header: { Text("Nova's hub") } footer: {
                    Text("The token is stored in this iPad's Keychain. Also configurable with novaminion://setup?hub=…&node=…&token=…")
                }
                Section("Status") {
                    LabeledContent("Connection", value: fleet.isOffline ? "resting (\(fleet.lastError ?? "no reply"))" : "live")
                    LabeledContent("Last heard", value: fleet.lastSuccess.map { Fmt.clock($0) } ?? "never")
                    LabeledContent("Minions", value: "\(fleet.snapshot?.fleet.count ?? 0)")
                }
                Section {
                    Text("Tip: Settings › Accessibility › Guided Access keeps the iPad locked to Nova Minion on its stand.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Nova Minion")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel", action: done) }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        draft.save(); config = .current; fleet.restart(); done()
                    }
                }
            }
        }
        .preferredColorScheme(.dark)
    }
}
