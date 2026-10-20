//
//  NodeDetailView.swift
//  Nova Cluster (iPad + Mac + Apple TV)
//
//  Machine drill-down (1.1.0): tap a station on the map, a dashboard card or a minion
//  card and this sheet shows that machine — what it is for, its LLM job, the wired link
//  and the Wi-Fi backdoor, temperatures, the UPS it hangs off, and its last 10 jobs.
//  Display-only, like the rest of the app; it re-reads the live snapshot every poll.
//

import SwiftUI

/// "Open this machine's drill-down", available anywhere in the view tree.
/// Equatable and always equal: ContentView rebuilds the closure on every render, and an environment value
/// SwiftUI can't compare is treated as changed, re-checking every reader each frame (1.1.0 dev: 80% CPU).
struct SelectNodeAction: Equatable {
    var run: (String) -> Void = { _ in }
    func callAsFunction(_ node: String) { run(node) }
    static func == (_: SelectNodeAction, _: SelectNodeAction) -> Bool { true }
}

private struct SelectNodeKey: EnvironmentKey {
    static let defaultValue = SelectNodeAction()
}

extension EnvironmentValues {
    var selectNode: SelectNodeAction {
        get { self[SelectNodeKey.self] }
        set { self[SelectNodeKey.self] = newValue }
    }
}

/// Identifiable wrapper for `.sheet(item:)`.
struct SelectedNode: Identifiable, Equatable {
    var name: String
    var id: String { name }
}

struct NodeDetailView: View {
    let cluster: ClusterModel
    let name: String
    let done: () -> Void

    private var node: ClusterSnapshot.Node? { cluster.snapshot?.allNodes.first { $0.node == name } }
    private var ups: ClusterSnapshot.UPS? { NodeFacts.ups(for: name, in: cluster.snapshot?.ups) }

    var body: some View {
        #if os(macOS)
        content.frame(minWidth: 560, minHeight: 620)
        #else
        NavigationStack {
            content
                .navigationTitle(name)
                #if os(iOS)
                .navigationBarTitleDisplayMode(.inline)
                #endif
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done", action: done) } }
        }
        #endif
    }

    private var content: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                if let n = node {
                    section("Now") {
                        let st = NodeLines.state(n)
                        row(st == .running ? "▸" : "·", NodeLines.job(n), tint: NodeLines.jobColor(st))
                        row("◆", NodeLines.purpose(n), tint: Palette.cool)
                    }
                    section("Link") {
                        let eth = NodeFacts.ethernet(n.net), wifi = NodeFacts.backdoor(n.net)
                        levelRow("Ethernet", eth.0, eth.1)
                        levelRow("Wi-Fi backdoor", wifi.0, wifi.1)
                        if let r = NodeFacts.route(n.net) { levelRow("Route", r.0, r.1) }
                    }
                    if let t = NodeFacts.temps(n.temps) {
                        section("Temperatures") { levelRow("Sensors", t.0, t.1) }
                    }
                    if let u = ups {
                        section("Power") {
                            let s = NodeFacts.upsSummary(u)
                            levelRow("\(u.label ?? u.key) UPS", s.0, s.1)
                        }
                    }
                    section("Machine") {
                        row("", "\(Fmt.chip(n.chip)) · \(Fmt.os(n.os)) · \(Int(n.cores ?? 0)) cores", tint: .white.opacity(0.75))
                        row("", "\(Fmt.gb(n.free_gb))/\(Fmt.gb(n.mem_gb)) GB free · load \(Fmt.one(n.load))", tint: .white.opacity(0.75))
                        let models = (n.loaded ?? []).map { ModelHue.short($0.name) }
                        row("", models.isEmpty ? "no models loaded" : models.joined(separator: " · "), tint: .white.opacity(0.6))
                    }
                    section("Last 10 jobs") {
                        let jobs = n.recent_jobs ?? []
                        if jobs.isEmpty {
                            row("", "nothing recorded yet", tint: .white.opacity(0.4))
                        } else {
                            ForEach(Array(jobs.prefix(10).enumerated()), id: \.offset) { _, j in
                                HStack(alignment: .firstTextBaseline, spacing: 10) {
                                    Text(Fmt.clock(Fmt.date(j.at)))
                                        .font(.system(size: 12, design: .monospaced))
                                        .foregroundStyle(.white.opacity(0.4))
                                        .frame(width: 72, alignment: .leading)
                                    Text(NodeFacts.jobIcon(j.kind)).frame(width: 18)
                                    Text(NodeLines.clean(j.text))
                                        .font(.system(size: 13, design: .monospaced))
                                        .foregroundStyle(Palette.terminal.opacity(0.9))
                                        .lineLimit(2)
                                }
                            }
                        }
                    }
                } else {
                    Text("This machine isn't in the latest snapshot.")
                        .foregroundStyle(.white.opacity(0.5))
                }
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Palette.space)
        .preferredColorScheme(.dark)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(NodeLines.clean(name)).font(.system(size: 24, weight: .semibold, design: .monospaced))
                if let a = node.flatMap(NodeLines.alias) {
                    Text("(\(a))").font(.system(size: 15, design: .monospaced)).foregroundStyle(.white.opacity(0.5))
                }
                Spacer()
                #if os(macOS)
                Button("Done", action: done).keyboardShortcut(.cancelAction)
                #endif
            }
            if let n = node {
                Text(n.isLive ? "probe live" : "probe silent · \(Fmt.ago(n.age_s ?? 0))")
                    .font(.system(size: 12, weight: .medium)).tracking(1.4)
                    .foregroundStyle(n.isLive ? Color.green.opacity(0.8) : Palette.alert)
            }
        }
    }

    private func section<C: View>(_ title: String, @ViewBuilder _ c: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title.uppercased()).font(.system(size: 11, weight: .semibold)).tracking(2.2)
                .foregroundStyle(Palette.warm.opacity(0.8))
            c()
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color.white.opacity(0.04)))
        #if os(tvOS)
        .focusable()
        #endif
    }

    private func row(_ icon: String, _ text: String, tint: Color) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            if !icon.isEmpty { Text(icon).foregroundStyle(tint.opacity(0.8)) }
            Text(NodeLines.clean(text)).font(.system(size: 14, design: .monospaced)).foregroundStyle(tint)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func levelRow(_ label: String, _ text: String, _ level: NodeFacts.Level) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Circle().fill(color(level)).frame(width: 8, height: 8)
            Text(label).font(.system(size: 13, weight: .medium)).foregroundStyle(.white.opacity(0.6))
                .frame(width: 150, alignment: .leading)
            Text(NodeLines.clean(text)).font(.system(size: 14, design: .monospaced)).foregroundStyle(.white.opacity(0.88))
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func color(_ l: NodeFacts.Level) -> Color {
        switch l {
        case .good: return Color(red: 0.42, green: 0.95, blue: 0.62)
        case .warn: return Palette.warm
        case .bad: return Palette.alert
        case .quiet: return .white.opacity(0.35)
        }
    }
}

/// The UPS on the map (bottom-left of the map area): one line per UPS.
struct UPSBadge: View {
    let list: [ClusterSnapshot.UPS]
    let u: CGFloat

    var body: some View {
        VStack(alignment: .leading, spacing: 4 * u) {
            ForEach(list) { ups in
                let s = NodeFacts.upsSummary(ups)
                HStack(spacing: 7 * u) {
                    Text("⚡").font(.system(size: 13 * u))
                    Text("\(ups.label ?? ups.key) UPS").font(.system(size: 11 * u, weight: .semibold)).tracking(1.2 * u)
                        .foregroundStyle(Palette.warm.opacity(0.9))
                    Text(NodeLines.clean(s.0)).font(.system(size: 11 * u, design: .monospaced))
                        .foregroundStyle(s.1 == .good ? .white.opacity(0.7) : (s.1 == .bad ? Palette.alert : Palette.warm))
                }
                .lineLimit(1)
            }
        }
        .padding(.horizontal, 10 * u).padding(.vertical, 7 * u)
        .background(RoundedRectangle(cornerRadius: 9 * u).fill(Color.black.opacity(0.55)))
        .accessibilityElement(children: .combine)
    }
}

/// Top-centre strip (every view): only when something is failing. Cycles through the problems; tap for the list.
struct AlertStrip: View {
    let alerts: ClusterSnapshot.Alerts?
    let u: CGFloat
    @State private var showAll = false
    @State private var index = 0

    var body: some View {
        let problems = AlertText.problems(alerts)
        if let head = AlertText.headline(alerts) {
            let bad = AlertText.level(alerts) == .bad
            let tint = bad ? Palette.alert : Palette.warm
            Button { showAll = true } label: {
                HStack(spacing: 8 * u) {
                    Circle().fill(tint).frame(width: 7 * u, height: 7 * u).shadow(color: tint, radius: 3 * u)
                    Text(head.uppercased()).font(.system(size: 10.5 * u, weight: .semibold)).tracking(1.4 * u)
                        .foregroundStyle(tint)
                    let item = problems[index % max(1, problems.count)]
                    Text(NodeLines.clean(AlertText.line(item), max: 90))
                        .font(.system(size: 11 * u, design: .monospaced)).foregroundStyle(.white.opacity(0.7))
                        .id(index).transition(.opacity)
                }
                .lineLimit(1)
                .padding(.horizontal, 12 * u).padding(.vertical, 5 * u)
                .background(Capsule().fill(Color.black.opacity(0.6)))
                .overlay(Capsule().strokeBorder(tint.opacity(0.4), lineWidth: 1))
            }
            .buttonStyle(.plain)
            .task(id: problems.count) {
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(5))
                    withAnimation(.easeInOut(duration: 0.4)) { index += 1 }
                }
            }
            .sheet(isPresented: $showAll) { AlertsSheet(alerts: alerts) { showAll = false } }
            .accessibilityLabel("Scheduler alerts: \(head)")
        }
    }
}

struct AlertsSheet: View {
    let alerts: ClusterSnapshot.Alerts?
    let done: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Scheduler alerts").font(.system(size: 22, weight: .semibold))
                Spacer()
                Button("Done", action: done).keyboardShortcut(.cancelAction)
            }
            if let l = alerts?.leader {
                Text("scheduler-core leader: \(l)").font(.system(size: 13, design: .monospaced)).foregroundStyle(.white.opacity(0.6))
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(Array((alerts?.items ?? []).enumerated()), id: \.offset) { _, i in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Circle().fill(i.level == "bad" ? Palette.alert : i.level == "warn" ? Palette.warm : .white.opacity(0.35))
                                .frame(width: 8, height: 8)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(NodeLines.clean(AlertText.line(i))).font(.system(size: 14, design: .monospaced))
                                if let r = i.reason, !r.isEmpty {
                                    Text(NodeLines.clean(r)).font(.system(size: 12, design: .monospaced))
                                        .foregroundStyle(.white.opacity(0.45))
                                }
                            }
                        }
                        #if os(tvOS)
                        .focusable()
                        #endif
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(22)
        #if os(macOS)
        .frame(minWidth: 620, minHeight: 420)
        #endif
        .background(Palette.space)
        .preferredColorScheme(.dark)
    }
}

/// "JUST PUBLISHED" ticker (under the alert strip): steps through Nova's newest posts; click / tap opens one.
struct PublishedTicker: View {
    let posts: [ClusterSnapshot.Post]
    let u: CGFloat
    @State private var index = 0
    @Environment(\.openURL) private var openURL

    var body: some View {
        if !posts.isEmpty {
            let p = posts[index % posts.count]
            Button {
                #if !os(tvOS)
                if let url = PublishedText.link(p) { openURL(url) }
                #endif
            } label: {
                HStack(spacing: 8 * u) {
                    Text("JUST PUBLISHED").font(.system(size: 9.5 * u, weight: .semibold)).tracking(1.6 * u)
                        .foregroundStyle(Palette.terminal.opacity(0.85))
                    Text(NodeLines.clean(PublishedText.line(p), max: 110))
                        .font(.system(size: 11 * u)).foregroundStyle(.white.opacity(0.75))
                        .id(p.key).transition(.opacity)
                }
                .lineLimit(1)
                .padding(.horizontal, 12 * u).padding(.vertical, 4 * u)
                .background(Capsule().fill(Color.black.opacity(0.5)))
            }
            .buttonStyle(.plain)
            #if os(macOS)
            .help(PublishedText.link(p)?.absoluteString ?? "")
            #endif
            .task(id: posts.first?.key) {
                index = 0                                          // a new post jumps to the front
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(8))
                    withAnimation(.easeInOut(duration: 0.4)) { index += 1 }
                }
            }
            .accessibilityLabel("Just published: \(p.title ?? "")")
        }
    }
}
