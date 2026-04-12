// NovaAPIServer.swift — Nova Desktop
// Port 37450 — aggregated Nova health API.
// Written by Jordan Koch.

import Foundation
import Network

@MainActor
final class NovaAPIServer {
    static let shared = NovaAPIServer()
    private var listener: NWListener?
    private let port: UInt16 = 37450
    private let queue = DispatchQueue(label: "net.digitalnoise.novadesktop.api", qos: .utility)
    private init() {}

    func start() {
        do {
            let params = NWParameters.tcp
            params.requiredLocalEndpoint = NWEndpoint.hostPort(
                host: NWEndpoint.Host("127.0.0.1"),
                port: NWEndpoint.Port(rawValue: port)!)
            listener = try NWListener(using: params)
        } catch {
            NSLog("[NovaAPIServer] Failed: \(error)"); return
        }
        listener?.newConnectionHandler = { [weak self] c in Task { @MainActor [weak self] in self?.handle(c) } }
        listener?.stateUpdateHandler = { state in
            if case .ready = state { NSLog("[NovaAPIServer] Listening on 127.0.0.1:37450") }
        }
        listener?.start(queue: queue)
    }

    func stop() { listener?.cancel(); listener = nil }

    private func handle(_ c: NWConnection) {
        c.start(queue: queue)
        c.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, _, _ in
            guard let self, let data, let raw = String(data: data, encoding: .utf8) else { c.cancel(); return }
            let lines = raw.components(separatedBy: "\r\n")
            guard let parts = lines.first?.split(separator: " "), parts.count >= 2 else { c.cancel(); return }
            let method = String(parts[0]); let path = String(parts[1]).components(separatedBy: "?").first ?? "/"
            if method == "OPTIONS" { self.send(c, 200, "{}"); return }
            Task { @MainActor in self.route(method, path, c) }
        }
    }

    @MainActor
    private func route(_ method: String, _ path: String, _ c: NWConnection) {
        let monitor = NovaMonitor.shared
        switch (method, path) {

        case ("GET", "/api/status"):
            let onlineCount = (monitor.aiServices + monitor.apps).filter { $0.state == .online }.count
            let totalCount  = monitor.aiServices.count + monitor.apps.count
            send(c, 200, json([
                "app": "Nova-Desktop", "port": 37450, "version": "1.0.0",
                "lastRefresh": ISO8601DateFormatter().string(from: monitor.lastRefresh),
                "servicesOnline": onlineCount, "servicesTotal": totalCount,
                "openClawGateway": monitor.openClaw.gatewayOnline,
                "memoryServer": monitor.openClaw.memoryServerOnline,
                "memoriesCount": monitor.openClaw.memoriesCount,
                "currentModel": monitor.openClaw.currentModel,
                "cronJobs": monitor.openClaw.cronJobs.count,
                "cronErrors": monitor.openClaw.cronJobs.filter { $0.state == "error" }.count
            ] as [String: Any]))

        case ("GET", "/api/health"):
            let checks: [[String: Any]] = [
                ["name": "OpenClawGateway", "ok": monitor.openClaw.gatewayOnline],
                ["name": "MemoryServer",    "ok": monitor.openClaw.memoryServerOnline],
                ["name": "Ollama",          "ok": monitor.aiServices.first { $0.id == "ollama" }?.state == .online],
                ["name": "MLXCode",         "ok": monitor.aiServices.first { $0.id == "mlxcode" }?.state == .online],
                ["name": "NovaControl",     "ok": monitor.apps.first { $0.id == "novacontrol" }?.state == .online],
            ]
            let allOk = checks.allSatisfy { ($0["ok"] as? Bool) == true }
            send(c, allOk ? 200 : 207, json(["healthy": allOk, "checks": checks, "timestamp": ISO8601DateFormatter().string(from: Date())] as [String: Any]))

        case ("GET", "/api/services"):
            let services = (monitor.aiServices + monitor.apps).map { s -> [String: Any] in
                ["id": s.id, "name": s.name, "state": s.state.rawValue,
                 "detail": s.detail, "latencyMs": s.latencyMs as Any, "port": s.port as Any]
            }
            send(c, 200, jsonArray(services))

        case ("GET", "/api/crons"):
            let crons = monitor.openClaw.cronJobs.map { j -> [String: Any] in
                ["id": j.id, "name": j.name, "state": j.state, "lastRun": j.lastRun, "nextRun": j.nextRun, "consecutiveErrors": j.consecutiveErrors]
            }
            send(c, 200, jsonArray(crons))

        case ("GET", "/api/github"):
            let repos = monitor.githubRepos.map { r -> [String: Any] in
                ["name": r.name, "fullName": r.fullName, "lastCommit": r.lastCommitMessage,
                 "openIssues": r.openIssues, "openPRs": r.openPRs, "stars": r.stars]
            }
            send(c, 200, jsonArray(repos))

        case ("POST", "/api/refresh"):
            Task { @MainActor in await NovaMonitor.shared.refresh() }
            send(c, 200, json(["status": "refresh_triggered"] as [String: Any]))

        default:
            send(c, 404, json(["error": "Not found: \(method) \(path)"] as [String: Any]))
        }
    }

    private func json(_ d: [String: Any]) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: d, options: .prettyPrinted),
              let str = String(data: data, encoding: .utf8) else { return "{}" }
        return str
    }
    private func jsonArray(_ a: [[String: Any]]) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: a, options: .prettyPrinted),
              let str = String(data: data, encoding: .utf8) else { return "[]" }
        return str
    }
    private func send(_ c: NWConnection, _ status: Int, _ body: String) {
        let st = [200:"OK",201:"Created",207:"Multi-Status",400:"Bad Request",404:"Not Found"][status] ?? "Unknown"
        let response = "HTTP/1.1 \(status) \(st)\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: \(body.utf8.count)\r\nAccess-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n\(body)"
        c.send(content: response.data(using: .utf8), completion: .contentProcessed { _ in c.cancel() })
    }
}
