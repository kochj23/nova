//
//  Fleet.swift
//  Nova Minion (iPad)
//
//  /fleet payload from Nova's minion hub, and the poller that keeps it fresh.
//  Display-only: the iPad never computes anything, it just shows the fleet.
//

import Foundation
import Observation

struct FleetSnapshot: Codable, Equatable {
    struct Tonight: Codable, Equatable { var works: Int?; var lessons: Int?; var minions: Int? }
    struct Lesson: Codable, Equatable, Hashable {
        var title: String
        var kind: String?
        var lesson: String
        var node: String?
        var at: String?
        var key: String { title + "\u{1F}" + lesson }
    }
    struct Reading: Codable, Equatable, Hashable {
        var title: String
        var kind: String?
        var phase: String?
        var started: String?
    }
    struct Minion: Codable, Equatable, Identifiable {
        var node: String
        var state: String?
        var since: String?
        var dedicated: Bool?
        var os: String?
        var tok_s: Double?
        var now_reading: [Reading]?
        var tonight: Tonight?
        var id: String { node }
    }

    var updated: String?
    var fleet_tonight: Tonight?
    var recent_lessons: [Lesson]?
    var queue: [String: Int]?
    var eta_days: Double?
    var minions: [Minion]?

    var lessons: [Lesson] { recent_lessons ?? [] }
    var fleet: [Minion] { minions ?? [] }
}

/// Lesson text as the hub writes it can carry citation markers like "[3][12]"; strip them.
enum LessonText {
    private static let citations = try! NSRegularExpression(pattern: #"\s*\[\d+(?:\s*[,–-]\s*\d+)*\]"#)
    static func clean(_ s: String) -> String {
        let r = NSRange(s.startIndex..., in: s)
        let stripped = citations.stringByReplacingMatches(in: s, range: r, withTemplate: "")
        return stripped.split(whereSeparator: \.isWhitespace).joined(separator: " ")
            .replacingOccurrences(of: " .", with: ".")
            .replacingOccurrences(of: " ,", with: ",")
    }
}

@MainActor
@Observable
final class FleetModel {
    private(set) var snapshot: FleetSnapshot?
    private(set) var lastSuccess: Date?
    private(set) var lastError: String?
    private(set) var failures = 0

    /// Offline after a few consecutive misses (or never reached at all).
    var isOffline: Bool { failures >= 3 || (snapshot == nil && failures > 0) }

    private var task: Task<Void, Never>?
    private static let cacheKey = "fleet.cache.v1"

    init() {
        if let data = UserDefaults.standard.data(forKey: Self.cacheKey),
           let snap = try? JSONDecoder().decode(FleetSnapshot.self, from: data) {
            snapshot = demoize(snap)
        }
    }

    func start() {
        guard task == nil else { return }
        task = Task { [weak self] in
            while !Task.isCancelled {
                await self?.pollOnce()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    func stop() { task?.cancel(); task = nil }

    func restart() { stop(); failures = 0; start() }

    func pollOnce() async {
        let cfg = MinionConfig.current
        guard let url = cfg.fleetURL else {
            failures += 1; lastError = "not configured"; return
        }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 4)
        if !cfg.token.isEmpty { req.setValue(cfg.token, forHTTPHeaderField: "X-Minion-Token") }
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else {
                throw URLError(.badServerResponse)
            }
            let snap = try JSONDecoder().decode(FleetSnapshot.self, from: data)
            let shown = demoize(snap)
            if shown != snapshot { snapshot = shown }
            UserDefaults.standard.set(data, forKey: Self.cacheKey)
            failures = 0; lastError = nil; lastSuccess = Date()
        } catch {
            failures += 1
            lastError = (error as? URLError)?.code == .badServerResponse ? "hub refused" : error.localizedDescription
        }
    }

    /// Debug/preview: `-demoMinions N` fakes extra minions (and spreads lessons across them)
    /// so the layout can be checked with a full fleet.
    private func demoize(_ snap: FleetSnapshot) -> FleetSnapshot {
        let n = UserDefaults.standard.integer(forKey: "demoMinions")
        guard n > 1 else { return snap }
        var s = snap
        let names = ["nova-core10", "studio-m3", "mini-garage", "air-den", "nas-attic"]
        let states = ["dedicated", "idle", "away", "offline", "dedicated"]
        let base = snap.fleet.first
        var minions: [FleetSnapshot.Minion] = []
        for i in 0..<min(n, names.count) {
            var m = base ?? FleetSnapshot.Minion(node: names[i])
            m.node = names[i]
            m.state = states[i]
            m.dedicated = states[i] == "dedicated"
            m.tok_s = states[i] == "offline" ? nil : (m.tok_s ?? 40) * [1, 1.6, 0.55, 0.8, 1.2][i]
            if i > 0 {
                let r = m.now_reading ?? []
                m.now_reading = states[i] == "offline" ? [] : Array(r.dropFirst(i).prefix(2))
                m.tonight = .init(works: (m.tonight?.works ?? 1) * (i + 2), lessons: (m.tonight?.lessons ?? 4) * (i + 3))
            }
            minions.append(m)
        }
        s.minions = minions
        s.recent_lessons = snap.lessons.enumerated().map { idx, l in
            var l = l
            let w = Int(StableHash.of(l.title, seed: 99) % UInt64(minions.count + 1))
            l.node = w == minions.count ? nil : minions[w].node
            _ = idx
            return l
        }
        s.fleet_tonight?.minions = minions.count
        return s
    }
}
