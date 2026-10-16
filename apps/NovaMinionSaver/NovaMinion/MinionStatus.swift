//
//  MinionStatus.swift
//  Nova Minion screensaver
//
//  Tolerant model of the minion agent's GET /status JSON. Every field is
//  optional: a missing or wrongly-typed field simply becomes nil so a partial
//  response still renders.
//

import Foundation

struct MinionLesson: Equatable {
    var title: String
    var kind: String?
    var lesson: String
    var node: String?
    var at: String?
    var mine: Bool

    /// Stable identity for de-duplication across polls.
    var key: String { (title + "\u{1}" + lesson).lowercased() }

    /// Lesson text with citation markers ([3], [12][14], [3, 4]) removed and whitespace tidied.
    var cleanText: String {
        var s = lesson.replacingOccurrences(of: #"\s*\[\d+(?:\s*,\s*\d+)*\]"#, with: "", options: .regularExpression)
        s = s.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
        s = s.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.count > 300 {
            let cut = s.prefix(300)
            if let space = cut.lastIndex(of: " ") { s = String(cut[..<space]) + "…" } else { s = String(cut) + "…" }
        }
        return s
    }
}

struct MinionReading: Equatable {
    var title: String
    var kind: String?
    var phase: String?
}

struct MinionStatus {
    var node: String?
    var state: String?
    var since: String?
    var nowReading: [MinionReading] = []
    var tonightWorks: Int?
    var tonightLessons: Int?
    var tonightBeliefs: Int?
    var fleetWorks: Int?
    var fleetLessons: Int?
    var fleetMinions: Int?
    var recentLessons: [MinionLesson] = []
    var tokS: Double?
    var model: String?
    var queue: [String: Int] = [:]
    var etaDays: Double?
    var updated: String?

    var isOffline: Bool { state?.lowercased() == "offline" }

    init() {}

    init?(data: Data) {
        guard let obj = try? JSONSerialization.jsonObject(with: data), let d = obj as? [String: Any] else { return nil }
        node = d["node"] as? String
        state = d["state"] as? String
        since = d["since"] as? String
        updated = d["updated"] as? String
        model = d["model"] as? String
        tokS = MinionStatus.num(d["tok_s"])
        etaDays = MinionStatus.num(d["eta_days"])
        if let t = d["tonight"] as? [String: Any] {
            tonightWorks = MinionStatus.int(t["works"])
            tonightLessons = MinionStatus.int(t["lessons"])
            tonightBeliefs = MinionStatus.int(t["beliefs"])
        }
        if let f = d["fleet_tonight"] as? [String: Any] {
            fleetWorks = MinionStatus.int(f["works"])
            fleetLessons = MinionStatus.int(f["lessons"])
            fleetMinions = MinionStatus.int(f["minions"])
        }
        if let q = d["queue"] as? [String: Any] {
            for (k, v) in q { if let n = MinionStatus.int(v) { queue[k] = n } }
        }
        if let arr = d["now_reading"] as? [[String: Any]] {
            nowReading = arr.compactMap { r in
                guard let title = r["title"] as? String, !title.isEmpty else { return nil }
                return MinionReading(title: title, kind: r["kind"] as? String, phase: r["phase"] as? String)
            }
        }
        if let arr = d["recent_lessons"] as? [[String: Any]] {
            recentLessons = arr.compactMap { r in
                guard let text = r["lesson"] as? String, !text.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
                let mine = (r["mine"] as? Bool) ?? ((r["mine"] as? NSNumber)?.boolValue ?? false)
                return MinionLesson(title: (r["title"] as? String) ?? "an untitled work",
                                    kind: r["kind"] as? String, lesson: text,
                                    node: r["node"] as? String, at: r["at"] as? String, mine: mine)
            }
        }
    }

    private static func num(_ v: Any?) -> Double? {
        if let n = v as? NSNumber { return n.doubleValue }
        if let s = v as? String { return Double(s) }
        return nil
    }

    private static func int(_ v: Any?) -> Int? { num(v).map { Int($0.rounded()) } }
}
