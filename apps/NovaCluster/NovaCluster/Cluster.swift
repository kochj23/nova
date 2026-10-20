//
//  Cluster.swift
//  Nova Cluster (iPad + Mac)
//
//  /cluster payload from Nova's hub (LLM traffic on every machine + the minions'
//  reading work), and the 2 s poller that keeps it fresh. Display-only.
//

import Foundation
import Observation

struct ClusterSnapshot: Codable, Equatable {
    struct Totals: Codable, Equatable {
        var nodes: Double?
        var models_loaded: Double?
        var vram_gb: Double?
        var busy_slots: Double?
        var tok_s: Double?
        var prefill_tok_s: Double?
        var req_min: Double?
        /// Decode + prefill tok/s across the cluster.
        var tok_s_total: Double?
        var today: Usage?
        var all_time: Usage?
    }
    struct Usage: Codable, Equatable {
        var requests: Double?
        var prompt_tokens: Double?
        var decoded_tokens: Double?
        var tokens: Double?
    }
    struct Loaded: Codable, Equatable {
        var name: String
        var port: Double?
        var size_gb: Double?
        var vram_gb: Double?
        var ctx: Double?
    }
    struct Slot: Codable, Equatable {
        var busy: Bool?
        var task: Double?
        var prompt: Double?
        var prompt_done: Double?
        var decoded: Double?
        var ctx: Double?
    }
    struct Runner: Codable, Equatable {
        var port: Double?
        var model: String?
        var ctx: Double?
        var standalone: Bool?
        var slots: [Slot]?
    }
    struct Tonight: Codable, Equatable {
        var works: Double?
        var lessons: Double?
        var minions: Double?
        var transcribed: Double?
        var audio_h: Double?
    }
    struct Reading: Codable, Equatable {
        var title: String?
        var kind: String?
        var phase: String?
        var started: String?
    }
    struct Minion: Codable, Equatable {
        var state: String?
        var dedicated: Bool?
        var tok_s: Double?
        var now_reading: [Reading]?
        var tonight: Tonight?
    }
    /// What the node's LLM is doing, as the hub words it (2026-10-17): always present on a current hub.
    /// state: running | idle | none (no LLM on the machine) | silent (probe not reporting).
    struct LLMJob: Codable, Equatable {
        struct Last: Codable, Equatable {
            var text: String?
            var at: String?
        }
        var state: String?
        var text: String?
        var jobs: [String]?
        var model: String?
        var last: Last?
    }
    struct Node: Codable, Equatable, Identifiable {
        var node: String
        var live: Bool?
        var age_s: Double?
        var os: String?
        var chip: String?
        var cores: Double?
        var mem_gb: Double?
        var free_gb: Double?
        var load: Double?
        var loaded: [Loaded]?
        var runners: [Runner]?
        var busy: Double?
        var tok_s: Double?
        var prefill_tok_s: Double?
        var req_min: Double?
        var vram_gb: Double?
        var role: String?
        var minion_state: String?
        var minion: Minion?
        var tok_s_total: Double?
        var today: Usage?
        /// Core purpose of the machine (minion_nodes.purpose, e.g. "PG standby · Mosquitto · embeddings").
        var purpose: String?
        /// The other name the machine goes by (mac-studio = nova-core8).
        var alias: String?
        var display_name: String?
        var ip: String?
        /// false when the probe found no LLM server on the machine at all.
        var llm: Bool?
        var llm_job: LLMJob?
        /// 1.1: the wired link and the Wi-Fi backdoor (probe 1.2), temperatures (°C, Linux only), last 10 jobs.
        var net: Net?
        var temps: [String: Double]?
        var recent_jobs: [RecentJob]?
        var id: String { node }
        var isLive: Bool { live ?? false }
    }
    /// Probe 1.2's link view: the default-route interface, the wired port, and the Wi-Fi backdoor.
    struct Net: Codable, Equatable {
        struct Link: Codable, Equatable {
            var iface: String?
            var up: Bool?
            var speed_mbps: Double?
            var ip: String?
        }
        var `default`: String?
        var eth: Link?
        var wifi: Link?
    }
    struct RecentJob: Codable, Equatable {
        var at: String?
        /// llm | reading | transcribing
        var kind: String?
        var text: String?
    }
    /// A UPS the hub reads from NUT (service_config service='ups').
    struct UPS: Codable, Equatable, Identifiable {
        var key: String
        var label: String?
        var model: String?
        var monitor_node: String?
        var powers: [String]?
        var ok: Bool?
        var error: String?
        var status: String?
        var on_battery: Bool?
        var low_battery: Bool?
        var online: Bool?
        var load_pct: Double?
        var watts: Double?
        var charge_pct: Double?
        var runtime_s: Double?
        var input_v: Double?
        var at: String?
        var id: String { key }
    }
    /// Scheduler alerts from the hub (1.1): failing / dead-letter / quarantined tasks and leader changes.
    struct Alerts: Codable, Equatable {
        struct Item: Codable, Equatable {
            /// bad | warn | info
            var level: String?
            /// dead-letter | quarantined | failing | unreachable | no-leader | failover | leader
            var kind: String?
            var source: String?
            var task: String?
            var text: String?
            var reason: String?
            var failures: Double?
            var at: String?
        }
        var items: [Item]?
        var counts: [String: Double]?
        var leader: String?
        var at: String?
    }
    /// A post just published on nova.digitalnoise.net (1.1).
    struct Post: Codable, Equatable {
        var title: String?
        var section: String?
        var section_name: String?
        var emoji: String?
        var url: String?
        var at: String?
        var key: String { url ?? title ?? "" }
    }
    struct Event: Codable, Equatable {
        var node: String?
        var model: String?
        var prompt: Double?
        var decoded: Double?
        var at: String?
        var key: String { "\(node ?? "")|\(model ?? "")|\(Int(prompt ?? 0))|\(Int(decoded ?? 0))|\(at ?? "")" }
        var tokens: Int { Int((prompt ?? 0) + (decoded ?? 0)) }
    }
    struct Lesson: Codable, Equatable {
        var title: String?
        var kind: String?
        var lesson: String?
        var node: String?
        var at: String?
        var key: String { (title ?? "") + "\u{1F}" + (lesson ?? "") }
    }

    var updated: String?
    var totals: Totals?
    var nodes: [Node]?
    var events: [Event]?
    var fleet_tonight: Tonight?
    var queue: [String: Double]?
    var eta_days: Double?
    var recent_lessons: [Lesson]?
    var ups: [UPS]?
    var alerts: Alerts?
    var published: [Post]?

    var allNodes: [Node] { nodes ?? [] }
    var allEvents: [Event] { events ?? [] }
    var lessons: [Lesson] { recent_lessons ?? [] }
}

/// A request a slot is serving right now.
struct ActiveRequest: Identifiable, Equatable {
    var node: String
    var model: String
    var prompt: Int
    var promptDone: Int
    var decoded: Int
    var id: String
    var generating: Bool { decoded > 0 }
    var prefillFraction: Double { prompt > 0 ? min(1, Double(promptDone) / Double(prompt)) : 0 }
}

extension ClusterSnapshot.Node {
    var activeRequests: [ActiveRequest] {
        var out: [ActiveRequest] = []
        for r in runners ?? [] {
            for (i, s) in (r.slots ?? []).enumerated() where s.busy == true {
                out.append(ActiveRequest(node: node, model: r.model ?? "?",
                                         prompt: Int(s.prompt ?? 0), promptDone: Int(s.prompt_done ?? 0),
                                         decoded: Int(s.decoded ?? 0),
                                         id: "\(node)|\(Int(r.port ?? 0))|\(i)"))
            }
        }
        return out
    }
}

@MainActor
@Observable
final class ClusterModel {
    private(set) var snapshot: ClusterSnapshot?
    private(set) var lastSuccess: Date?
    private(set) var lastError: String?
    private(set) var failures = 0

    /// Finished requests that arrived since the previous poll, to be fired as comets.
    /// Not observed — the map drains it every frame.
    @ObservationIgnored let comets = CometQueue()
    /// Smooth "tokens today" between polls.
    @ObservationIgnored let ticker = TokenTicker()

    var isOffline: Bool { failures >= 2 || (snapshot == nil && failures > 0) }

    private var task: Task<Void, Never>?
    @ObservationIgnored private var seenEvents = Set<String>()
    @ObservationIgnored private var primed = false
    @ObservationIgnored private let defaults: UserDefaults
    @ObservationIgnored private let session: URLSession
    @ObservationIgnored private let config: () -> ClusterConfig
    static let cacheKey = "cluster.cache.v1"

    /// `defaults`, `session` and `config` are injectable so tests use a throwaway suite and a stubbed network.
    init(defaults: UserDefaults = .standard, session: URLSession = .shared,
         config: @escaping () -> ClusterConfig = { ClusterConfig.current }) {
        self.defaults = defaults
        self.session = session
        self.config = config
        if let data = defaults.data(forKey: Self.cacheKey),
           let snap = try? JSONDecoder().decode(ClusterSnapshot.self, from: data) {
            snapshot = snap
        }
    }

    func start() {
        guard task == nil else { return }
        task = Task { [weak self] in
            while !Task.isCancelled {
                await self?.pollOnce()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    func stop() { task?.cancel(); task = nil }

    func restart() { stop(); failures = 0; start() }

    func pollOnce() async {
        let cfg = config()
        guard let url = cfg.clusterURL else {
            failures += 1; lastError = "not configured"; return
        }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 3)
        if !cfg.token.isEmpty { req.setValue(cfg.token, forHTTPHeaderField: "X-Minion-Token") }
        do {
            let (data, resp) = try await session.data(for: req)
            guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else {
                throw URLError(.badServerResponse)
            }
            var snap = try JSONDecoder().decode(ClusterSnapshot.self, from: data)
            if defaults.bool(forKey: "demoBusy") { snap = Self.demoBusy(snap) }
            ingestEvents(snap.allEvents)
            ticker.update(today: snap.totals?.today?.tokens, allTime: snap.totals?.all_time?.tokens,
                          rate: snap.totals?.tok_s_total ?? snap.totals?.tok_s)
            if snap != snapshot { snapshot = snap }
            if Int(Date().timeIntervalSince1970) % 30 < 2 {
                defaults.set(data, forKey: Self.cacheKey)
            }
            failures = 0; lastError = nil; lastSuccess = Date()
        } catch {
            failures += 1
            lastError = (error as? URLError)?.code == .badServerResponse ? "hub refused" : error.localizedDescription
        }
    }

    /// Debug: `-demoBusy YES` marks a few slots busy (prefill + generating) to check the visuals.
    private static func demoBusy(_ s: ClusterSnapshot) -> ClusterSnapshot {
        var s = s
        let t = Date().timeIntervalSince1970
        for (i, n) in s.allNodes.enumerated() where i < 4 {
            guard var runners = n.runners, !runners.isEmpty, var slots = runners[0].slots, !slots.isEmpty else { continue }
            let p = 4000.0, phase = (t / 2).truncatingRemainder(dividingBy: 40) + Double(i) * 9
            slots[0].busy = true; slots[0].prompt = p
            slots[0].prompt_done = min(p, phase * 400); slots[0].decoded = max(0, phase * 400 - p) / 10
            runners[0].slots = slots
            s.nodes?[i].runners = runners
        }
        return s
    }

    /// New finished requests become comets, spread across the poll interval so traffic
    /// reads as a continuous stream rather than a 2 s pulse. The first poll only primes.
    private func ingestEvents(_ events: [ClusterSnapshot.Event]) {
        var fresh: [ClusterSnapshot.Event] = []
        for e in events where !seenEvents.contains(e.key) {
            seenEvents.insert(e.key)
            fresh.append(e)
        }
        if seenEvents.count > 1200 { seenEvents = Set(events.map(\.key)) }
        guard primed else { primed = true; return }
        let now = Date()
        let ordered = Array(fresh.reversed().suffix(24))      // oldest first
        let gap = ordered.count > 1 ? 1.8 / Double(ordered.count) : 0
        for (i, e) in ordered.enumerated() {
            comets.push(Comet(node: e.node ?? "", model: e.model ?? "", tokens: e.tokens,
                              key: e.key, born: now.addingTimeInterval(Double(i) * gap)))
        }
    }
}

/// Cumulative token counter that ticks up between polls by extrapolating the cluster's
/// current tok/s, eases up to the server's figure at each poll, and never counts backwards
/// (except at the daily reset, when the server's "today" falls back to near zero).
@MainActor
final class TokenTicker {
    private var base = 0.0          // server's tokens-today at the last poll
    private var allBase = 0.0       // server's all-time at the last poll
    private var at = Date.distantPast
    private var rate = 0.0
    private var shown = 0.0
    private var lastFrame = Date()
    private(set) var hasData = false

    func update(today: Double?, allTime: Double?, rate: Double?) {
        guard let today else { return }
        if hasData && today < shown * 0.5 && shown - today > 5000 { shown = today }   // new day
        if !hasData { shown = today }
        base = today; allBase = allTime ?? today
        at = Date(); self.rate = max(0, rate ?? 0); hasData = true
    }

    /// Tokens today as of `now` (call once per frame; monotonic).
    func today(at now: Date) -> Double {
        let dt = max(0, min(0.5, now.timeIntervalSince(lastFrame)))
        lastFrame = now
        // Extrapolate at most ~3 s past the last poll so a stalled hub can't run away.
        let target = base + rate * min(3, max(0, now.timeIntervalSince(at)))
        if target > shown {
            // Ease toward the target: fast enough to keep up, smooth enough to read.
            shown += max((target - shown) * min(1, dt * 5), min(target - shown, rate * dt))
        }
        return shown
    }

    func allTime(at now: Date) -> Double { allBase + max(0, shown - base) }
    var currentRate: Double { rate }
}

struct Comet {
    var node: String
    var model: String
    var tokens: Int
    var key: String
    var born: Date
}

@MainActor
final class CometQueue {
    private(set) var items: [Comet] = []
    func push(_ c: Comet) { items.append(c); if items.count > 80 { items.removeFirst(items.count - 80) } }
    func prune(now: Date, life: Double) { items.removeAll { now.timeIntervalSince($0.born) > life } }
}
