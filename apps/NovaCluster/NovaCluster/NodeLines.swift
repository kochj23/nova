//
//  NodeLines.swift
//  Nova Cluster (iPad + Mac)
//
//  The two lines every machine carries in both views (Jordan, 2026-10-17: "Always add in what job the
//  LLM is running and then, on a different line, the node's core purpose"):
//    1. the LLM job — what the hub says the machine's LLM is doing right now (the job that sent the
//       request, or Idle / No LLM / Silent). Never invented here: the hub names jobs from minion_activity
//       and the probes' connection tables; an older hub falls back to what the snapshot itself shows.
//    2. the core purpose — minion_nodes.purpose ("PG standby · Mosquitto · embeddings").
//  Both are single fixed-height lines (truncated; full text on hover / tap / long-press), so the
//  layout never moves when the text changes.
//

import SwiftUI

enum JobState: String {
    case running, idle, none, silent
}

enum NodeLines {
    static let maxLength = 240

    /// Hub text is data from the LAN, not markup: one line, no control or bidi-override characters, bounded.
    static func clean(_ s: String?, max: Int = maxLength) -> String {
        guard let s else { return "" }
        let banned = CharacterSet.controlCharacters
            .union(CharacterSet(charactersIn: "\u{202A}\u{202B}\u{202C}\u{202D}\u{202E}\u{2066}\u{2067}\u{2068}\u{2069}\u{200E}\u{200F}"))
        var out = String(String.UnicodeScalarView(s.unicodeScalars.map { banned.contains($0) ? " " : $0 }))
        while out.contains("  ") { out = out.replacingOccurrences(of: "  ", with: " ") }
        out = out.trimmingCharacters(in: .whitespaces)
        if out.count > max { out = String(out.prefix(max - 1)) + "…" }
        return out
    }

    /// "mac-studio (nova-core8)" — the hub's display name, else node + alias, else the node id.
    static func displayName(_ n: ClusterSnapshot.Node) -> String {
        let d = clean(n.display_name, max: 64)
        if !d.isEmpty { return d }
        let a = clean(n.alias, max: 32)
        return a.isEmpty ? n.node : "\(n.node) (\(a))"
    }

    static func alias(_ n: ClusterSnapshot.Node) -> String? {
        let a = clean(n.alias, max: 32)
        return a.isEmpty ? nil : a
    }

    static func purpose(_ n: ClusterSnapshot.Node) -> String {
        let p = clean(n.purpose)
        return p.isEmpty ? "purpose not set" : p
    }

    static func state(_ n: ClusterSnapshot.Node) -> JobState {
        if let s = n.llm_job?.state, let st = JobState(rawValue: s) { return st }
        if !n.isLive { return .silent }
        if !n.activeRequests.isEmpty || !(n.minion?.now_reading ?? []).isEmpty { return .running }
        if n.llm == false && (n.loaded ?? []).isEmpty { return .none }
        return .idle
    }

    /// Line 1: always one line of text, never empty.
    static func job(_ n: ClusterSnapshot.Node) -> String {
        let hub = clean(n.llm_job?.text)
        if !hub.isEmpty { return hub }
        // An older hub without llm_job: say only what the snapshot shows.
        if !n.isLive { return "Silent · probe last heard \(Fmt.ago(n.age_s ?? 0)) ago" }
        var jobs: [String] = (n.minion?.now_reading ?? []).map { r in
            let phase = (r.phase ?? "reading").lowercased()
            return "\(phase == "transcribing" ? "transcribing (whisper)" : phase): \(clean(r.title, max: 80))"
        }
        if let r = n.activeRequests.max(by: { $0.decoded < $1.decoded }) {
            let phase = r.generating ? "generating \(Fmt.int(r.decoded)) tok" : "prefill \(Int(r.prefillFraction * 100))%"
            jobs.append(jobs.isEmpty ? "\(r.model) · \(phase) · caller not identified" : "\(r.model) · \(phase)")
        }
        if !jobs.isEmpty { return clean(jobs.joined(separator: " → ")) }
        if n.llm == false && (n.loaded ?? []).isEmpty { return "No LLM on this node" }
        let k = (n.loaded ?? []).count
        return k == 0 ? "Idle · no model loaded" : "Idle · \(Fmt.plural(k, "model", "models")) loaded"
    }

    static func jobColor(_ st: JobState) -> Color {
        switch st {
        case .running: return Palette.terminal
        case .idle: return .white.opacity(0.55)
        case .none: return .white.opacity(0.38)
        case .silent: return Palette.alert.opacity(0.85)
        }
    }

    /// The full text for a tooltip / long-press: name, job, purpose (untruncated, still cleaned).
    static func detail(_ n: ClusterSnapshot.Node) -> String {
        var lines = [displayName(n), "LLM: " + job(n), "Purpose: " + purpose(n)]
        if let ip = n.ip.map({ clean($0, max: 45) }), !ip.isEmpty { lines.append(ip) }
        return lines.joined(separator: "\n")
    }
}

/// The two fixed lines: ▸ LLM job, then the core purpose. Same height whatever the text.
struct NodeRoleLines: View {
    let node: ClusterSnapshot.Node
    let u: CGFloat
    var size: CGFloat = 11
    var alignment: HorizontalAlignment = .leading

    static func lineHeight(size: CGFloat, u: CGFloat) -> CGFloat { ceil(size * u * 1.36) }

    var body: some View {
        let st = NodeLines.state(node)
        let h = Self.lineHeight(size: size, u: u)
        let frameAlign = Alignment(horizontal: alignment, vertical: .center)
        VStack(alignment: alignment, spacing: 2 * u) {
            Text("▸ " + NodeLines.job(node))
                .font(.system(size: size * u, weight: st == .running ? .semibold : .regular, design: .monospaced))
                .foregroundStyle(NodeLines.jobColor(st))
                .lineLimit(1).truncationMode(.tail)
                .frame(maxWidth: .infinity, minHeight: h, maxHeight: h, alignment: frameAlign)
                .accessibilityIdentifier("node.job.\(node.node)")
            Text(NodeLines.purpose(node))
                .font(.system(size: size * u, weight: .medium))
                .foregroundStyle(Palette.cool.opacity(node.purpose == nil ? 0.4 : 0.8))
                .lineLimit(1).truncationMode(.tail)
                .frame(maxWidth: .infinity, minHeight: h, maxHeight: h, alignment: frameAlign)
                .accessibilityIdentifier("node.purpose.\(node.node)")
        }
        .help(NodeLines.detail(node))
        .accessibilityElement(children: .combine)
    }
}
