//
//  NodeFacts.swift
//  Nova Cluster (iPad + Mac + Apple TV)
//
//  Pure wording for the machine drill-down and the UPS badge (1.1.0), kept apart from
//  SwiftUI so the unit tests can check every line the sheet shows.
//

import Foundation

enum NodeFacts {
    enum Level: Equatable { case good, warn, bad, quiet }

    /// "1 Gb/s", "2.5 Gb/s", "100 Mb/s".
    static func speed(_ mbps: Double?) -> String? {
        guard let m = mbps, m > 0 else { return nil }
        if m >= 1000 {
            let g = m / 1000
            return (g == g.rounded() ? String(Int(g)) : g.formatted(.number.precision(.fractionLength(1)))) + " Gb/s"
        }
        return "\(Int(m)) Mb/s"
    }

    /// Wired port: "eno1 · up · 1 Gb/s · 192.168.1.125" / "en0 · DOWN" / "no wired port reported".
    static func ethernet(_ n: ClusterSnapshot.Net?) -> (String, Level) {
        guard let e = n?.eth else { return ("no wired port reported", .quiet) }
        let name = e.iface ?? "ethernet"
        guard e.up == true else { return ("\(name) · DOWN", .bad) }
        var parts = [name, "up"]
        if let s = speed(e.speed_mbps) { parts.append(s) }
        if let ip = e.ip { parts.append(ip) }
        return (parts.joined(separator: " · "), .good)
    }

    /// The Wi-Fi backdoor (Jordan 2026-10-19: every node on KOCH_5G-2 as a way back in).
    static func backdoor(_ n: ClusterSnapshot.Net?) -> (String, Level) {
        guard let w = n?.wifi else { return ("no Wi-Fi backdoor", .warn) }
        let name = w.iface ?? "wifi"
        guard w.up == true, let ip = w.ip else { return ("\(name) · DOWN (no backdoor)", .bad) }
        return ("\(name) · up · \(ip)", .good)
    }

    /// Which way traffic leaves now ("via Wi-Fi" is worth a warning: the cable has dropped).
    static func route(_ n: ClusterSnapshot.Net?) -> (String, Level)? {
        guard let d = n?.default else { return nil }
        if d == n?.wifi?.iface { return ("default route via Wi-Fi (\(d)): the wired link is down or demoted", .warn) }
        return ("default route via \(d)", .quiet)
    }

    /// "CPU 52 °C · GPU 43 °C · NVMe 34 °C"; hot readings flagged.
    static func temps(_ t: [String: Double]?) -> (String, Level)? {
        guard let t, !t.isEmpty else { return nil }
        let order = [("cpu", "CPU", 85.0), ("gpu", "GPU", 85.0), ("nvme", "NVMe", 70.0), ("nic", "NIC", 95.0)]
        var parts: [String] = [], hot = false
        for (k, name, limit) in order {
            guard let v = t[k] else { continue }
            parts.append("\(name) \(Int(v.rounded())) °C")
            if v >= limit { hot = true }
        }
        return parts.isEmpty ? nil : (parts.joined(separator: " · "), hot ? .warn : .good)
    }

    /// "29 min", "1 h 05 min", "45 s".
    static func runtime(_ s: Double?) -> String? {
        guard let s, s >= 0 else { return nil }
        if s < 60 { return "\(Int(s)) s" }
        let m = Int(s / 60)
        return m < 60 ? "\(m) min" : String(format: "%d h %02d min", m / 60, m % 60)
    }

    /// The UPS this machine is plugged into, if the hub knows one.
    static func ups(for node: String, in list: [ClusterSnapshot.UPS]?) -> ClusterSnapshot.UPS? {
        list?.first { ($0.powers ?? []).contains(node) }
    }

    /// "18% load · 162 W · 100% · 30 min · on line" (or on battery / unreachable).
    static func upsSummary(_ u: ClusterSnapshot.UPS) -> (String, Level) {
        guard u.ok == true else { return ("unreachable" + (u.error.map { " (\($0))" } ?? ""), .warn) }
        var parts: [String] = []
        if let l = u.load_pct { parts.append("\(Int(l))% load") }
        if let w = u.watts { parts.append("\(Int(w)) W") }
        if let c = u.charge_pct { parts.append("\(Int(c))%") }
        if let r = runtime(u.runtime_s) { parts.append(r) }
        let state: String, level: Level
        if u.low_battery == true { state = "LOW BATTERY"; level = .bad }
        else if u.on_battery == true { state = "ON BATTERY"; level = .bad }
        else if u.online == true { state = "on line"; level = .good }
        else { state = u.status ?? "unknown"; level = .warn }
        parts.append(state)
        return (parts.joined(separator: " · "), level)
    }

    static func jobIcon(_ kind: String?) -> String {
        switch kind {
        case "reading": return "📖"
        case "transcribing": return "🎙"
        default: return "›"
        }
    }
}

/// Wording for the alert strip (1.1.0).
enum AlertText {
    typealias Item = ClusterSnapshot.Alerts.Item

    /// The items the strip cycles through: problems only (bad first), never the info-level leader history.
    static func problems(_ a: ClusterSnapshot.Alerts?) -> [Item] {
        (a?.items ?? []).filter { $0.level == "bad" || $0.level == "warn" }
    }

    /// "2 dead-letter · 3 failing" / "scheduler-core on standby"; nil when everything is fine.
    static func headline(_ a: ClusterSnapshot.Alerts?) -> String? {
        let p = problems(a)
        guard !p.isEmpty else { return nil }
        var parts: [String] = []
        let dead = p.filter { ["dead-letter", "quarantined"].contains($0.kind ?? "") }.count
        let failing = p.filter { $0.kind == "failing" }.count
        let other = p.count - dead - failing
        if dead > 0 { parts.append("\(dead) dead-letter") }
        if failing > 0 { parts.append("\(failing) failing") }
        if other > 0 { parts.append(other == 1 ? (p.first { !["dead-letter", "quarantined", "failing"].contains($0.kind ?? "") }?.kind ?? "alert") : "\(other) more") }
        return parts.joined(separator: " · ")
    }

    static func level(_ a: ClusterSnapshot.Alerts?) -> NodeFacts.Level {
        let p = problems(a)
        if p.contains(where: { $0.level == "bad" }) { return .bad }
        return p.isEmpty ? .good : .warn
    }

    /// One strip line: "studio · model_warm: quarantined after 10 failures in a row".
    static func line(_ i: Item) -> String {
        [i.source, i.text].compactMap { $0 }.joined(separator: " · ")
    }
}

/// Wording for the "just published" ticker (1.1.0).
enum PublishedText {
    /// Only links to Nova's own site are opened (the hub's data is LAN data, not trusted markup).
    static func link(_ p: ClusterSnapshot.Post) -> URL? {
        guard let s = p.url, let u = URL(string: s), u.scheme == "https", u.host == "nova.digitalnoise.net" else { return nil }
        return u
    }

    /// "🔍 Belief and Friends: Garments … · 19:13".
    static func line(_ p: ClusterSnapshot.Post, clock: (Date?) -> String = Fmt.clock) -> String {
        var s = "\(p.emoji ?? "•") \(p.title ?? "untitled")"
        let c = clock(Fmt.date(p.at))
        if !c.isEmpty { s += " · \(c)" }
        return s
    }
}
