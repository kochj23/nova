//
//  GamesModel.swift
//  Nova Cluster (iPad + Mac + Apple TV)
//
//  1.3.0: the Games screen's data: the hub's /games payload, the poller, and display helpers.
//

import Foundation
import Observation

// MARK: - Payload (hub GET /games)

struct GamesSnapshot: Codable, Equatable {
    struct DB: Codable, Equatable { var up: Bool?; var ms: Double?; var host: String?; var error: String? }
    struct Session: Codable, Equatable, Identifiable {
        var id: String
        var game: String?; var client: String?; var player: String?
        var started: String?; var last_seen: String?; var ended: String?
        var outcome: String?; var score: Double?; var actions: Int?
        var who: String?; var what: String?
    }
    struct Game: Codable, Equatable, Identifiable {
        var game: String; var sessions: Int?; var today: Int?; var last: String?
        var id: String { game }
    }
    struct Events: Codable, Equatable { var day: Int?; var hour: Int? }
    struct AICall: Codable, Equatable {
        var t: String?; var game: String?; var role: String?; var backend: String?; var model: String?
        var ok: Bool?; var ms: Double?; var tokens_out: Int?; var tok_s: Double?
    }
    struct AIToday: Codable, Equatable { var calls: Int?; var tokens_out: Int?; var tok_s: Double? }
    struct AI: Codable, Equatable { var calls: [AICall]?; var today: AIToday? }
    struct Relay: Codable, Equatable {
        var status: String?; var ok: Int?; var fallback: Int?; var rejected: Int?
        var in_flight: Int?; var polls_failed: Int?; var yielding: String?
    }
    struct Running: Codable, Equatable { var node: String; var jobs: [String]?; var model: String?; var phase: String?; var tok_s: Double? }
    struct Recent: Codable, Equatable { var node: String; var text: String; var at: String? }
    struct LLM: Codable, Equatable { var running: [Running]?; var tok_s: Double?; var recent: [Recent]? }

    var db: DB?
    var live: [Session]?
    var recent: [Session]?
    var games: [Game]?
    var events: Events?
    var ai: AI?
    var relay: Relay?
    var llm: LLM?
    var updated: String?
}

/// Friendly names for the platform's game ids.
enum GameNames {
    static func display(_ id: String?) -> String {
        switch id ?? "" {
        case "gtnw": return "GTNW"
        case "casting": return "Casting Director"
        case "blompie", "blompie-mac": return "Blompie"
        case "cartest": return "CarTest"
        case "stuntdriver": return "Stunt Driver"
        case "mush": return "Nova MUSH"
        case let other: return other.isEmpty ? "—" : other
        }
    }

    /// "4m ago" from an ISO-8601 timestamp (hub times carry a UTC offset).
    static func ago(_ iso: String?, now: Date = Date()) -> String {
        guard let iso, let d = parse(iso) else { return "—" }
        let s = max(0, now.timeIntervalSince(d))
        if s < 90 { return "\(Int(s))s ago" }
        if s < 5400 { return "\(Int(s / 60))m ago" }
        if s < 172_800 { return "\(Int(s / 3600))h ago" }
        return "\(Int(s / 86400))d ago"
    }

    static func parse(_ iso: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: iso) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: iso)
    }
}

// MARK: - Model

@Observable
final class GamesModel {
    private(set) var snapshot: GamesSnapshot?
    private(set) var lastError: String?
    @ObservationIgnored private var task: Task<Void, Never>?
    @ObservationIgnored private let session: URLSession
    @ObservationIgnored private let config: () -> ClusterConfig

    init(session: URLSession = .shared, config: @escaping () -> ClusterConfig = { ClusterConfig.current }) {
        self.session = session
        self.config = config
    }

    /// Polls only while the Games screen is on screen (onAppear / onDisappear).
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

    func pollOnce() async {
        let cfg = config()
        guard let url = cfg.gamesURL else { lastError = "not configured"; return }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 5)
        if !cfg.token.isEmpty { req.setValue(cfg.token, forHTTPHeaderField: "X-Minion-Token") }
        do {
            let (data, resp) = try await session.data(for: req)
            guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else { throw URLError(.badServerResponse) }
            let snap = try JSONDecoder().decode(GamesSnapshot.self, from: data)
            if snap != snapshot { snapshot = snap }
            lastError = nil
        } catch {
            lastError = (error as? URLError)?.code == .badServerResponse ? "hub has no /games yet" : error.localizedDescription
        }
    }
}
