//
//  ConstellationFleet.swift
//  Nova Cluster (iPad + Mac + Apple TV)
//
//  Pure helpers behind the Constellation view's minion cards (kept out of the SwiftUI
//  file so the unit tests can compile them without a window).
//

import Foundation

/// Which machines show as minions on the constellation, and what their card says.
enum ConstellationFleet {
    /// Machines doing minion work (reading, mapping, transcribing), hub-sent order kept.
    static func minions(_ s: ClusterSnapshot?) -> [ClusterSnapshot.Node] {
        (s?.allNodes ?? []).filter { $0.minion != nil }
    }

    /// "81 works · 106 lessons", "4 episodes transcribed · 1.6 h audio", or both.
    static func tonightParts(_ t: ClusterSnapshot.Tonight?) -> [String] {
        let works = Int(t?.works ?? 0), lessons = Int(t?.lessons ?? 0), eps = Int(t?.transcribed ?? 0)
        let w = Fmt.plural(works, "work", "works"), l = Fmt.plural(lessons, "lesson", "lessons")
        guard eps > 0 else { return [w, l] }
        if works == 0 { return ["\(Fmt.plural(eps, "episode", "episodes")) transcribed", "\(Fmt.one(t?.audio_h)) h audio"] }
        return ["\(w) · \(l)", Fmt.plural(eps, "episode", "episodes")]
    }

    static func readingText(_ r: ClusterSnapshot.Reading) -> String {
        let p = (r.phase ?? "").lowercased()
        let title = r.title ?? "…"
        if p == "transcribing" { return "transcribing: \(title)" }
        var s = "reading: \(title)"
        if let k = r.kind, !k.isEmpty { s += " (\(k))" }
        if !p.isEmpty { s += " · \(p)…" }
        return s
    }
}
