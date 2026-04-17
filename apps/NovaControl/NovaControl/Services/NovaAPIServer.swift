// NovaControl — Unified HTTP API Server
// Written by Jordan Koch
// Port 37400 · binds to 127.0.0.1 only
// Replaces: OneOnOne (37421), NMAPScanner (37423), RsyncGUI (37424), TopGUI (37443), News Summary (37438)

import CryptoKit
import Foundation
import Network

final class NovaAPIServer {
    static let shared = NovaAPIServer()

    private var listener: NWListener?
    private let port: UInt16 = 37400
    private let queue = DispatchQueue(label: "net.digitalnoise.novacontrol.apiserver", qos: .utility)

    // In-memory health status (POST /api/health/status)
    private var latestHealthStatus: HealthStatusRecord?

    /// Local-only anti-CSRF bearer token (not a secret — just prevents drive-by POST from browser JS)
    private let apiToken: String = {
        let key = "NovaAPIToken"
        if let existing = UserDefaults.standard.string(forKey: key), !existing.isEmpty {
            return existing
        }
        let token = UUID().uuidString
        UserDefaults.standard.set(token, forKey: key)
        return token
    }()

    private init() {}

    // MARK: - Start / Stop

    func start() {
        do {
            let params = NWParameters.tcp
            guard let nwPort = NWEndpoint.Port(rawValue: port) else {
                NSLog("[NovaAPIServer] Invalid port: \(port)")
                return
            }
            params.requiredLocalEndpoint = NWEndpoint.hostPort(
                host: NWEndpoint.Host("127.0.0.1"),
                port: nwPort
            )
            listener = try NWListener(using: params)
        } catch {
            NSLog("[NovaAPIServer] Failed to create listener: \(error)")
            return
        }

        listener?.newConnectionHandler = { [weak self] connection in
            self?.handleConnection(connection)
        }

        listener?.stateUpdateHandler = { state in
            switch state {
            case .ready:
                NSLog("[NovaAPIServer] Listening on 127.0.0.1:37400")
            case .failed(let error):
                NSLog("[NovaAPIServer] Listener failed: \(error)")
            default:
                break
            }
        }

        listener?.start(queue: queue)
    }

    func stop() {
        listener?.cancel()
        listener = nil
    }

    // MARK: - Connection Handling

    private func handleConnection(_ connection: NWConnection) {
        connection.start(queue: queue)
        receiveRequest(from: connection)
    }

    private func receiveRequest(from connection: NWConnection) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, isComplete, error in
            guard let self = self else { return }
            if let data = data, !data.isEmpty {
                self.processRequest(data: data, connection: connection)
            }
            if isComplete || error != nil {
                connection.cancel()
            }
        }
    }

    private func processRequest(data: Data, connection: NWConnection) {
        guard let requestString = String(data: data, encoding: .utf8) else {
            sendError(connection: connection, status: 400, message: "Bad request")
            return
        }

        let lines = requestString.components(separatedBy: "\r\n")
        guard let requestLine = lines.first else {
            sendError(connection: connection, status: 400, message: "Bad request")
            return
        }

        let parts = requestLine.split(separator: " ")
        guard parts.count >= 2 else {
            sendError(connection: connection, status: 400, message: "Bad request")
            return
        }

        let method = String(parts[0]).uppercased()
        let rawPath = String(parts[1])

        // Parse path and query string
        let pathComponents = rawPath.split(separator: "?", maxSplits: 1)
        let path = String(pathComponents[0])
        var queryParams: [String: String] = [:]
        if pathComponents.count > 1 {
            let queryString = String(pathComponents[1])
            for pair in queryString.split(separator: "&") {
                let kv = pair.split(separator: "=", maxSplits: 1)
                if kv.count == 2 {
                    queryParams[String(kv[0])] = String(kv[1]).removingPercentEncoding ?? String(kv[1])
                }
            }
        }

        // Parse request headers
        var ifNoneMatch: String?
        var authorization: String?
        for line in lines.dropFirst() where !line.isEmpty {
            guard let colonIdx = line.firstIndex(of: ":") else { continue }
            let key = String(line[..<colonIdx]).lowercased().trimmingCharacters(in: .whitespaces)
            let value = String(line[line.index(after: colonIdx)...]).trimmingCharacters(in: .whitespaces)
            if key == "if-none-match" { ifNoneMatch = value }
            if key == "authorization" { authorization = value }
        }

        // Parse body for POST requests
        var body: Data?
        if method == "POST", let bodyRange = requestString.range(of: "\r\n\r\n") {
            let bodyStart = requestString.distance(from: requestString.startIndex, to: bodyRange.upperBound)
            if let bodyData = requestString.dropFirst(bodyStart).data(using: .utf8) {
                body = bodyData
            }
        }

        // Route the request
        route(method: method, path: path, query: queryParams, body: body,
              ifNoneMatch: ifNoneMatch, authorization: authorization, connection: connection)
    }

    // MARK: - Router

    private func route(method: String, path: String, query: [String: String],
                       body: Data?, ifNoneMatch: String?, authorization: String?,
                       connection: NWConnection) {
        if method == "OPTIONS" {
            sendResponse(connection: connection, status: 200, body: Data())
            return
        }

        // Require bearer token for all POST requests (anti-CSRF)
        if method == "POST" {
            guard let auth = authorization, auth == "Bearer \(apiToken)" else {
                sendError(connection: connection, status: 401, message: "Unauthorized — missing or invalid Bearer token")
                return
            }
        }

        // Prometheus metrics — plain text response
        if method == "GET" && path == "/metrics" {
            Task {
                let text = await self.buildPrometheusMetrics()
                let data = text.data(using: .utf8) ?? Data()
                self.sendResponse(connection: connection, status: 200, body: data,
                                  contentType: "text/plain; version=0.0.4")
            }
            return
        }

        Task {
            let (status, responseBody) = await self.handleRoute(
                method: method, path: path, query: query, body: body)

            // ETag support: compute hash of serialized JSON for GET 200 responses
            // .sortedKeys ensures stable ordering so the same data always hashes identically
            if method == "GET", status == 200,
               let jsonData = try? JSONSerialization.data(withJSONObject: responseBody,
                                                          options: [.prettyPrinted, .sortedKeys]) {
                let etag = self.computeETag(data: jsonData)
                if let clientETag = ifNoneMatch, clientETag == etag {
                    self.sendResponse(connection: connection, status: 304, body: Data(),
                                      extraHeaders: ["ETag": etag])
                } else {
                    self.sendResponse(connection: connection, status: status, body: jsonData,
                                      extraHeaders: ["ETag": etag])
                }
            } else {
                self.sendJSON(connection: connection, status: status, json: responseBody)
            }
        }
    }

    private func computeETag(data: Data) -> String {
        let hash = SHA256.hash(data: data)
        let hex  = hash.compactMap { String(format: "%02x", $0) }.joined()
        return "\"\(hex)\""
    }

    private func handleRoute(method: String, path: String, query: [String: String], body: Data?) async -> (Int, Any) {
        // GET /api/status
        if method == "GET" && path == "/api/status" {
            return await handleStatus()
        }

        // OneOnOne routes
        if method == "GET" && path == "/api/oneonone/meetings" {
            return await handleMeetings(query: query)
        }
        if method == "GET" && path == "/api/oneonone/actionitems" {
            return await handleActionItems(query: query)
        }
        if method == "GET" && path == "/api/oneonone/people" {
            return await handlePeople()
        }

        // NMAP routes
        if method == "GET" && path == "/api/nmap/devices" {
            return await handleDevices()
        }
        if method == "GET" && path == "/api/nmap/threats" {
            return await handleThreats()
        }
        if method == "POST" && path == "/api/nmap/scan" {
            return await handleNmapScan(body: body)
        }

        // Rsync routes
        if method == "GET" && path == "/api/rsync/jobs" {
            return await handleRsyncJobs()
        }
        if method == "GET" && path == "/api/rsync/history" {
            return await handleRsyncHistory()
        }
        // POST /api/rsync/jobs/{id}/run
        if method == "POST" && path.hasPrefix("/api/rsync/jobs/") && path.hasSuffix("/run") {
            let idString = path
                .replacingOccurrences(of: "/api/rsync/jobs/", with: "")
                .replacingOccurrences(of: "/run", with: "")
            return await handleRsyncRun(jobIdString: idString)
        }

        // System routes
        if method == "GET" && path == "/api/system/stats" {
            return await handleSystemStats()
        }
        if method == "GET" && path == "/api/system/processes" {
            return await handleProcesses()
        }

        // News routes
        if method == "GET" && path == "/api/news/breaking" {
            return await handleBreakingNews()
        }
        if method == "GET" && path == "/api/news/favorites" {
            return await handleNewsFavorites()
        }
        if method == "GET" && path.hasPrefix("/api/news/articles/") {
            let category = path.replacingOccurrences(of: "/api/news/articles/", with: "")
            return await handleNewsByCategory(category: category)
        }

        // OneOnOne goals
        if method == "GET" && path == "/api/oneonone/goals" {
            return await handleGoals()
        }

        // Nova routes
        if method == "GET" && path == "/api/nova/status" {
            return await handleNovaStatus()
        }
        if method == "GET" && path == "/api/nova/memory" {
            return await handleNovaMemory()
        }
        if method == "GET" && path == "/api/nova/crons" {
            return await handleNovaCrons()
        }

        // AI services
        if method == "GET" && path == "/api/ai/status" {
            return await handleAIStatus()
        }
        if method == "GET" && path == "/api/ai/llms" {
            return await handleLocalLLMs()
        }

        // MLXCode proxy
        if method == "GET" && path == "/api/mlxcode/status" {
            return await handleMLXCodeStatus()
        }
        if method == "GET" && path.hasPrefix("/api/mlxcode/") {
            let proxied = path.replacingOccurrences(of: "/api/mlxcode", with: "")
            return await handleMLXCodeProxy(path: proxied)
        }

        // Topology
        if method == "GET" && path == "/api/topology" {
            return await handleTopology()
        }

        // Manual health status
        if method == "GET" && path == "/api/health/status" {
            return handleHealthStatusGet()
        }
        if method == "POST" && path == "/api/health/status" {
            return handleHealthStatusPost(body: body)
        }

        // Comprehensive healthcheck
        if method == "GET" && path == "/api/health" {
            return await handleHealthCheck()
        }

        // Goals insights
        if method == "GET" && path == "/api/oneonone/goals/insights" {
            return await handleGoalInsights()
        }

        // Workflow automation
        if method == "GET" && path == "/api/workflows" {
            return await handleWorkflowList()
        }
        if method == "POST" && path.hasPrefix("/api/workflows/") && path.hasSuffix("/run") {
            let wid = path.replacingOccurrences(of: "/api/workflows/", with: "").replacingOccurrences(of: "/run", with: "")
            return await handleWorkflowRun(id: wid, body: body)
        }
        if method == "GET" && path == "/api/workflows/runs" {
            return await handleWorkflowRuns()
        }

        // Centralized logs
        if method == "GET" && path == "/api/logs" {
            return handleLogs(query: query)
        }

        // OpenAPI documentation
        if method == "GET" && path == "/api/docs" {
            return handleDocs()
        }

        // Content graph (stub — connect Neo4j for full implementation)
        if method == "GET" && path == "/api/graph" {
            return await handleContentGraph()
        }

        return (404, ["error": "Route not found", "path": path])
    }

    // MARK: - Route Handlers

    private func handleStatus() async -> (Int, Any) {
        let dm = await DataManager.shared
        let statuses = await dm.serviceStatuses
        let lastRefresh = await dm.lastRefresh

        // Probe Nova memory gateway
        var novaMemoryStatus = "unreachable"
        if let url = URL(string: "http://127.0.0.1:18790/health") {
            do {
                let (_, response) = try await URLSession.shared.data(from: url)
                if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                    novaMemoryStatus = "online"
                }
            } catch {
                // unreachable — leave as is
            }
        }

        let statusDicts = statuses.map { s -> [String: Any] in
            [
                "id": s.id,
                "name": s.name,
                "oldPort": s.oldPort,
                "status": s.status.rawValue,
                "summary": s.summary,
                "lastUpdated": ISO8601DateFormatter().string(from: s.lastUpdated)
            ]
        }

        return (200, [
            "novacontrol": "online",
            "port": 37400,
            "lastRefresh": ISO8601DateFormatter().string(from: lastRefresh),
            "novaMemoryGateway": novaMemoryStatus,
            "services": statusDicts
        ])
    }

    private func handleMeetings(query: [String: String]) async -> (Int, Any) {
        let meetings = await OneOnOneReader.shared.fetchMeetings()
        let limit = Int(query["limit"] ?? "20") ?? 20
        let limited = Array(meetings.sorted { $0.date > $1.date }.prefix(limit))
        return (200, encodable(limited))
    }

    private func handleActionItems(query: [String: String]) async -> (Int, Any) {
        let items = await OneOnOneReader.shared.fetchActionItems()
        let filtered: [ActionItem]
        if let completedStr = query["completed"] {
            let wantCompleted = completedStr.lowercased() == "true"
            filtered = items.filter { $0.isCompleted == wantCompleted }
        } else {
            filtered = items
        }
        return (200, encodable(filtered))
    }

    private func handlePeople() async -> (Int, Any) {
        let people = await OneOnOneReader.shared.fetchPeople()
        return (200, encodable(people))
    }

    private func handleDevices() async -> (Int, Any) {
        let devices = await NMAPReader.shared.fetchDevices()
        return (200, encodable(devices))
    }

    private func handleThreats() async -> (Int, Any) {
        let threats = await NMAPReader.shared.fetchThreats()
        return (200, encodable(threats))
    }

    private func handleNmapScan(body: Data?) async -> (Int, Any) {
        guard let body = body,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: String],
              let ip = json["ip"] else {
            return (400, ["error": "Request body must contain {\"ip\": \"...\"}"])
        }
        let result = await NMAPReader.shared.runScan(ip: ip)
        return (200, ["ip": ip, "result": result])
    }

    private func handleRsyncJobs() async -> (Int, Any) {
        let jobs = await RsyncReader.shared.fetchJobs()
        return (200, encodable(jobs))
    }

    private func handleRsyncHistory() async -> (Int, Any) {
        let history = await RsyncReader.shared.fetchHistory()
        return (200, encodable(history))
    }

    private func handleRsyncRun(jobIdString: String) async -> (Int, Any) {
        guard let jobId = UUID(uuidString: jobIdString) else {
            return (400, ["error": "Invalid job ID: \(jobIdString)"])
        }
        let result = await RsyncReader.shared.runJob(jobId)
        return (200, ["jobId": jobIdString, "output": result])
    }

    private func handleSystemStats() async -> (Int, Any) {
        let stats = await SystemStatsReader.shared.fetchStats()
        return (200, encodable(stats))
    }

    private func handleProcesses() async -> (Int, Any) {
        let processes = await SystemStatsReader.shared.fetchProcesses()
        return (200, encodable(processes))
    }

    private func handleBreakingNews() async -> (Int, Any) {
        let articles = await NewsSummaryReader.shared.fetchBreaking()
        return (200, encodable(articles))
    }

    private func handleNewsFavorites() async -> (Int, Any) {
        let articles = await NewsSummaryReader.shared.fetchFavorites()
        return (200, encodable(articles))
    }

    private func handleNewsByCategory(category: String) async -> (Int, Any) {
        let articles = await NewsSummaryReader.shared.fetchByCategory(category)
        return (200, encodable(articles))
    }

    private func handleGoals() async -> (Int, Any) {
        let goals = await OneOnOneReader.shared.fetchGoals()
        return (200, encodable(goals))
    }

    // MARK: - Nova Handlers

    private func handleNovaStatus() async -> (Int, Any) {
        let status = await NovaReader.shared.fetchStatus()
        return (200, encodable(status))
    }

    private func handleNovaMemory() async -> (Int, Any) {
        guard let url = URL(string: "http://127.0.0.1:18790/stats") else {
            return (503, ["error": "memory server unreachable"])
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.0
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse, http.statusCode == 200,
              let json = try? JSONSerialization.jsonObject(with: data) else {
            // Fall back to health endpoint
            if let url2 = URL(string: "http://127.0.0.1:18790/health"),
               let (data2, _) = try? await URLSession.shared.data(from: url2),
               let json2 = try? JSONSerialization.jsonObject(with: data2) {
                return (200, json2)
            }
            return (503, ["error": "memory server unreachable"])
        }
        return (http.statusCode, json)
    }

    private func handleNovaCrons() async -> (Int, Any) {
        let crons = await NovaReader.shared.fetchCrons()
        return (200, encodable(crons))
    }

    // MARK: - AI Services Handler

    private func handleAIStatus() async -> (Int, Any) {
        let services = await NovaReader.shared.fetchAIServices()
        let serviceDicts = services.map { svc -> [String: Any] in
            ["id": svc.id, "name": svc.name, "port": svc.port,
             "online": svc.isOnline, "detail": svc.detail]
        }
        let onlineCount = services.filter { $0.isOnline }.count
        return (200, [
            "services": serviceDicts,
            "onlineCount": onlineCount,
            "totalCount": services.count
        ])
    }

    private func handleLocalLLMs() async -> (Int, Any) {
        let llms = await NovaReader.shared.fetchLocalLLMs()
        let dicts = llms.map { llm -> [String: Any] in
            var d: [String: Any] = [
                "id": llm.id, "name": llm.name, "backend": llm.backend,
                "loaded": llm.isLoaded, "available": llm.isAvailable, "detail": llm.detail
            ]
            if let size = llm.sizeGB { d["size_gb"] = size }
            return d
        }
        let loaded = llms.filter(\.isLoaded).count
        return (200, ["models": dicts, "loaded": loaded, "total": llms.count])
    }

    // MARK: - MLXCode Handlers

    private func handleMLXCodeStatus() async -> (Int, Any) {
        let info = await MLXCodeReader.shared.fetchStatus()
        guard let info = info else {
            return (503, ["error": "MLXCode unreachable", "port": 37422])
        }
        return (200, encodable(info))
    }

    private func handleMLXCodeProxy(path: String) async -> (Int, Any) {
        let (status, body) = await MLXCodeReader.shared.proxy(path: path)
        return (status, body)
    }

    // MARK: - Workflow Handlers

    private func handleWorkflowList() async -> (Int, Any) {
        let defs = await MainActor.run { WorkflowEngine.shared.definitions }
        let list = defs.map { w -> [String: Any] in [
            "id": w.id, "name": w.name, "enabled": w.enabled,
            "stepCount": w.steps.count,
            "trigger": "\(w.trigger)"
        ]}
        return (200, ["workflows": list, "count": list.count])
    }

    private func handleWorkflowRun(id: String, body: Data?) async -> (Int, Any) {
        var context: [String: String] = ["trigger": "manual"]
        if let body = body,
           let extra = try? JSONSerialization.jsonObject(with: body) as? [String: String] {
            context.merge(extra) { $1 }
        }
        let run = await WorkflowEngine.shared.run(workflowId: id, context: context)
        let iso = ISO8601DateFormatter()
        return (run.status == .completed ? 200 : 500, [
            "runId": run.id.uuidString,
            "workflowId": run.workflowId,
            "status": run.status.rawValue,
            "triggeredAt": iso.string(from: run.triggeredAt),
            "completedAt": run.completedAt.map { iso.string(from: $0) } as Any,
            "error": run.error as Any,
            "steps": run.stepResults.map { r -> [String: Any] in
                ["stepId": r.stepId, "status": r.status, "output": r.output, "durationMs": r.durationMs]
            }
        ])
    }

    private func handleWorkflowRuns() async -> (Int, Any) {
        let (runs, total) = await MainActor.run {
            (WorkflowEngine.shared.recentRuns.prefix(20), WorkflowEngine.shared.recentRuns.count)
        }
        let iso = ISO8601DateFormatter()
        let list = runs.map { r -> [String: Any] in [
            "runId": r.id.uuidString, "workflowId": r.workflowId,
            "status": r.status.rawValue, "triggeredAt": iso.string(from: r.triggeredAt),
            "stepCount": r.stepResults.count, "error": r.error as Any
        ]}
        return (200, ["runs": list, "total": total])
    }

    // MARK: - Logs Handler

    // GET /api/logs?n=100&level=warn&source=nova_nightly_report&since=2026-04-15T00:00:00
    private func handleLogs(query: [String: String]) -> (Int, Any) {
        let n = Int(query["n"] ?? "100") ?? 100
        let logDir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".openclaw/logs", isDirectory: true)
        let logFile = logDir.appendingPathComponent("nova.jsonl")
        guard let data = try? String(contentsOf: logFile, encoding: .utf8) else {
            return (200, ["entries": [] as [Any], "total": 0])
        }
        let lines = data.split(separator: "\n").suffix(n).reversed()
        var entries: [[String: Any]] = []
        let levelFilter = query["level"]
        let sourceFilter = query["source"]
        let sinceFilter = query["since"]
        let levelOrder = ["debug": 0, "info": 1, "warn": 2, "error": 3, "fatal": 4]
        let minLevel = levelOrder[levelFilter ?? ""] ?? 0

        for line in lines {
            guard let lineData = line.data(using: .utf8),
                  let entry = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any] else { continue }
            if let lvl = entry["level"] as? String, (levelOrder[lvl] ?? 0) < minLevel { continue }
            if let src = sourceFilter, (entry["source"] as? String) != src { continue }
            if let since = sinceFilter, let ts = entry["ts"] as? String, ts < since { continue }
            entries.append(entry)
            if entries.count >= n { break }
        }
        return (200, ["entries": entries, "total": entries.count])
    }

    // MARK: - Documentation & Graph Handlers

    // GET /api/docs — OpenAPI 3.0 specification
    private func handleDocs() -> (Int, Any) {
        let spec: [String: Any] = [
            "openapi": "3.0.3",
            "info": ["title": "NovaControl API", "version": "1.0.0", "description": "Unified API for all Nova services · Jordan Koch · port 37400"],
            "servers": [["url": "http://127.0.0.1:37400", "description": "Local loopback only"]],
            "paths": [
                "/api/status":                     endpoint("GET", "Service status overview"),
                "/api/health":                     endpoint("GET", "Comprehensive healthcheck with per-source checks"),
                "/api/health/status":              endpointWithPost("GET", "POST", "Get / store manual health note"),
                "/api/topology":                   endpoint("GET", "Service communication topology graph"),
                "/api/graph":                      endpoint("GET", "Content relationship graph (Neo4j stub)"),
                "/api/docs":                       endpoint("GET", "This OpenAPI specification"),
                "/metrics":                        endpoint("GET", "Prometheus text metrics"),
                "/api/oneonone/meetings":          endpoint("GET", "OneOnOne meetings (limit query param)"),
                "/api/oneonone/actionitems":       endpoint("GET", "Action items (completed=true|false)"),
                "/api/oneonone/people":            endpoint("GET", "People directory"),
                "/api/oneonone/goals":             endpoint("GET", "Goals list"),
                "/api/oneonone/goals/insights":    endpoint("GET", "Goal insights with health correlation"),
                "/api/nmap/devices":               endpoint("GET", "Scanned network devices"),
                "/api/nmap/threats":               endpoint("GET", "Security threat findings"),
                "/api/nmap/scan":                  endpoint("POST", "Start an NMAP scan {\"ip\":\"...\"}"),
                "/api/rsync/jobs":                 endpoint("GET", "RsyncGUI sync jobs"),
                "/api/rsync/history":              endpoint("GET", "Rsync execution history"),
                "/api/rsync/jobs/{id}/run":        endpoint("POST", "Run a specific rsync job"),
                "/api/system/stats":               endpoint("GET", "CPU, RAM, disk I/O, uptime"),
                "/api/system/processes":           endpoint("GET", "Top 20 processes by CPU"),
                "/api/news/breaking":              endpoint("GET", "Breaking news articles"),
                "/api/news/favorites":             endpoint("GET", "Favourite articles"),
                "/api/news/articles/{category}":   endpoint("GET", "Articles by category"),
                "/api/nova/status":                endpoint("GET", "Nova gateway + memory status"),
                "/api/nova/memory":                endpoint("GET", "Nova vector memory stats"),
                "/api/nova/crons":                 endpoint("GET", "Nova cron job list"),
                "/api/ai/status":                  endpoint("GET", "AI service availability (Ollama, MLX, etc.)"),
                "/api/mlxcode/status":             endpoint("GET", "MLXCode proxy status"),
            ]
        ]
        return (200, spec)
    }

    private func endpoint(_ method: String, _ description: String) -> [String: Any] {
        [method.lowercased(): ["summary": description, "responses": ["200": ["description": "OK"]]]]
    }

    private func endpointWithPost(_ get: String, _ post: String, _ description: String) -> [String: Any] {
        [
            get.lowercased():  ["summary": description, "responses": ["200": ["description": "OK"]]],
            post.lowercased(): ["summary": description, "requestBody": ["required": true,
                "content": ["application/json": ["schema": ["type": "object"]]]],
                "responses": ["200": ["description": "OK"]]]
        ]
    }

    // GET /api/graph — Content relationship graph (Neo4j integration stub)
    // Full implementation: set up Neo4j and populate via /api/graph/ingest
    private func handleContentGraph() async -> (Int, Any) {
        let news    = await NewsSummaryReader.shared.fetchBreaking()
        let devices = await NMAPReader.shared.fetchDevices()

        // Build lightweight in-memory graph from available data.
        // Neo4j integration: replace with Bolt protocol queries once server is running.
        var nodes: [[String: Any]] = []
        var edges: [[String: Any]] = []

        // Service nodes
        let serviceNodes = ["OneOnOne", "NMAPScanner", "RsyncGUI", "SystemStats", "NewsSummary", "Nova", "MLXCode"]
        for name in serviceNodes {
            nodes.append(["id": name, "type": "service", "label": name])
        }

        // News category nodes derived from live data
        let categories = Set(news.map { $0.category })
        for cat in categories {
            let catId = "news:\(cat)"
            nodes.append(["id": catId, "type": "news_category", "label": cat])
            edges.append(["from": "NewsSummary", "to": catId, "relation": "contains"])
        }

        // Device type nodes
        let deviceTypes = Set(devices.map { $0.deviceType })
        for dt in deviceTypes.prefix(8) {
            let dtId = "device:\(dt)"
            nodes.append(["id": dtId, "type": "device_type", "label": dt])
            edges.append(["from": "NMAPScanner", "to": dtId, "relation": "discovered"])
        }

        return (200, [
            "nodes": nodes,
            "edges": edges,
            "nodeCount": nodes.count,
            "edgeCount": edges.count,
            "neo4jStatus": "not_connected",
            "note": "Connect Neo4j (bolt://localhost:7687) and POST /api/graph/ingest to enable full graph queries.",
            "generatedAt": ISO8601DateFormatter().string(from: Date())
        ])
    }

    // MARK: - New Feature Handlers

    // GET /api/topology — service communication graph
    private func handleTopology() async -> (Int, Any) {
        let devices   = await NMAPReader.shared.fetchDevices()
        let nova      = await NovaReader.shared.fetchStatus()
        let mlx       = await MLXCodeReader.shared.fetchStatus()
        let aiSvcs    = await NovaReader.shared.fetchAIServices()

        let novaOnline = nova.gatewayOnline
        let mlxOnline  = mlx != nil
        let nmapOnline = !devices.isEmpty

        func svcOnline(_ id: String) -> Bool {
            aiSvcs.first(where: { $0.id == id })?.isOnline ?? false
        }

        let connections: [[String: Any]] = [
            ["from": "NovaControl",  "to": "OneOnOne",      "type": "data_sync",     "active": true],
            ["from": "NovaControl",  "to": "NMAPScanner",   "type": "data_sync",     "active": nmapOnline],
            ["from": "NovaControl",  "to": "RsyncGUI",      "type": "data_sync",     "active": true],
            ["from": "NovaControl",  "to": "SystemStats",   "type": "data_sync",     "active": true],
            ["from": "NovaControl",  "to": "NewsSummary",   "type": "data_sync",     "active": true],
            ["from": "NovaControl",  "to": "Nova",          "type": "data_sync",     "active": novaOnline],
            ["from": "SystemStats",  "to": "NMAPScanner",   "type": "device_health", "active": nmapOnline],
            ["from": "Nova",         "to": "MLXCode",       "type": "ai_inference",  "active": novaOnline && mlxOnline],
            ["from": "Nova",         "to": "MemoryServer",  "type": "memory",        "active": novaOnline],
            ["from": "Nova",         "to": "Slack",         "type": "notifications", "active": novaOnline],
            ["from": "NewsSummary",  "to": "Nova",          "type": "data_sync",     "active": novaOnline],
        ]

        return (200, [
            "connections": connections,
            "generatedAt": ISO8601DateFormatter().string(from: Date())
        ])
    }

    // GET /api/health/status — retrieve stored manual health note
    private func handleHealthStatusGet() -> (Int, Any) {
        guard let record = latestHealthStatus else {
            return (200, [
                "memoryPressure": "normal",
                "notes": "",
                "recordedAt": ISO8601DateFormatter().string(from: Date()),
                "hasManualStatus": false
            ])
        }
        return (200, [
            "memoryPressure": record.memoryPressure,
            "notes": record.notes,
            "recordedAt": ISO8601DateFormatter().string(from: record.recordedAt),
            "hasManualStatus": true
        ])
    }

    // POST /api/health/status — store manual health note
    private func handleHealthStatusPost(body: Data?) -> (Int, Any) {
        guard let body = body,
              let input = try? JSONDecoder().decode(ManualHealthInput.self, from: body) else {
            return (400, ["error": "Request body must be JSON: {\"memory_pressure\": \"high\", \"notes\": \"...\"}"])
        }
        let pressure = input.memoryPressure ?? "normal"
        let validPressures = ["normal", "high", "critical"]
        guard validPressures.contains(pressure) else {
            return (400, ["error": "memory_pressure must be one of: normal, high, critical"])
        }
        let record = HealthStatusRecord(
            memoryPressure: pressure,
            notes: input.notes ?? "",
            recordedAt: Date()
        )
        latestHealthStatus = record
        return (200, [
            "ok": true,
            "memoryPressure": record.memoryPressure,
            "notes": record.notes,
            "recordedAt": ISO8601DateFormatter().string(from: record.recordedAt)
        ])
    }

    // GET /api/health — comprehensive data source healthcheck
    private func handleHealthCheck() async -> (Int, Any) {
        let nova    = await NovaReader.shared.fetchStatus()
        let devices = await NMAPReader.shared.fetchDevices()
        let stats   = await SystemStatsReader.shared.fetchStats()
        let mlx     = await MLXCodeReader.shared.fetchStatus()

        var checks: [[String: Any]] = [
            ["name": "NovaGateway",   "ok": nova.gatewayOnline,   "detail": nova.gatewayOnline ? "online" : "offline"],
            ["name": "MemoryServer",  "ok": nova.memoryServerOnline, "detail": nova.memoryServerOnline ? "\(nova.memoriesCount) memories" : "offline"],
            ["name": "NMAPScanner",   "ok": !devices.isEmpty,     "detail": "\(devices.count) devices"],
            ["name": "SystemStats",   "ok": true,                  "detail": String(format: "CPU %.0f%% · RAM %.1fGB", stats.cpuUser + stats.cpuSystem, stats.memUsedGB)],
            ["name": "MLXCode",       "ok": mlx != nil,            "detail": mlx?.activeModel ?? "offline"],
        ]

        // OneOnOne — check file access
        let meetings = await OneOnOneReader.shared.fetchMeetings()
        checks.append(["name": "OneOnOne", "ok": !meetings.isEmpty, "detail": "\(meetings.count) meetings"])

        let allOk = checks.allSatisfy { ($0["ok"] as? Bool) == true }
        let failCount = checks.filter { ($0["ok"] as? Bool) == false }.count

        return (allOk ? 200 : 207, [
            "healthy": allOk,
            "failingCount": failCount,
            "checks": checks,
            "timestamp": ISO8601DateFormatter().string(from: Date()),
            "manualNote": latestHealthStatus.map { ["pressure": $0.memoryPressure, "notes": $0.notes] } as Any
        ])
    }

    // GET /api/oneonone/goals/insights — goal completion trends & health correlation
    private func handleGoalInsights() async -> (Int, Any) {
        let goals   = await OneOnOneReader.shared.fetchGoals()
        let stats   = await SystemStatsReader.shared.fetchStats()

        // Status breakdown
        var statusCounts: [String: Int] = [:]
        for goal in goals {
            statusCounts[goal.status, default: 0] += 1
        }
        let breakdown = statusCounts.map { ["status": $0.key, "count": $0.value] }
            .sorted { ($0["count"] as? Int ?? 0) > ($1["count"] as? Int ?? 0) }

        let completed    = goals.filter { $0.status.lowercased() == "completed" }
        let completionRate = goals.isEmpty ? 0.0 : Double(completed.count) / Double(goals.count)

        // Health correlation note
        let cpu = stats.cpuUser + stats.cpuSystem
        let healthNote: String
        if let manual = latestHealthStatus, manual.memoryPressure == "critical" {
            healthNote = "Manual override: critical memory pressure — goal reviews may be delayed"
        } else if cpu > 80 {
            healthNote = "High CPU (\(Int(cpu))%) — system under load during this review"
        } else if stats.memUsedGB / stats.memTotalGB > 0.85 {
            healthNote = String(format: "Memory at %.0f%% — consider closing unused apps", (stats.memUsedGB / stats.memTotalGB) * 100)
        } else {
            healthNote = "System healthy — good time for goal review"
        }

        // Most recent completed goals (up to 5)
        let recentCompleted = completed.prefix(5).map { ["id": $0.id.uuidString, "title": $0.title] }

        return (200, [
            "totalGoals": goals.count,
            "completionRate": completionRate,
            "statusBreakdown": breakdown,
            "recentlyCompleted": recentCompleted,
            "healthCorrelation": healthNote,
            "generatedAt": ISO8601DateFormatter().string(from: Date())
        ])
    }

    // GET /metrics — Prometheus text format
    private func buildPrometheusMetrics() async -> String {
        let stats   = await SystemStatsReader.shared.fetchStats()
        let devices = await NMAPReader.shared.fetchDevices()
        let threats = await NMAPReader.shared.fetchThreats()
        let goals   = await OneOnOneReader.shared.fetchGoals()
        let nova    = await NovaReader.shared.fetchStatus()
        let items   = await OneOnOneReader.shared.fetchActionItems()

        var lines: [String] = []

        func gauge(_ name: String, _ help: String, _ value: Double, labels: String = "") {
            lines.append("# HELP \(name) \(help)")
            lines.append("# TYPE \(name) gauge")
            let labelStr = labels.isEmpty ? "" : "{\(labels)}"
            lines.append("\(name)\(labelStr) \(value)")
        }

        gauge("novacontrol_cpu_percent",      "CPU usage percent",          stats.cpuUser + stats.cpuSystem)
        gauge("novacontrol_cpu_user_percent", "CPU user percent",           stats.cpuUser,   labels: "mode=\"user\"")
        gauge("novacontrol_cpu_sys_percent",  "CPU system percent",         stats.cpuSystem, labels: "mode=\"system\"")
        gauge("novacontrol_mem_used_gb",      "Memory used in GB",          stats.memUsedGB)
        gauge("novacontrol_mem_total_gb",     "Memory total in GB",         stats.memTotalGB)
        gauge("novacontrol_disk_read_mbs",    "Disk read MB/s",             stats.diskReadMBs)
        gauge("novacontrol_disk_write_mbs",   "Disk write MB/s",            stats.diskWriteMBs)
        gauge("novacontrol_uptime_seconds",   "System uptime in seconds",   stats.uptime)
        gauge("novacontrol_nmap_devices",     "NMAP scanned device count",  Double(devices.count))
        gauge("novacontrol_nmap_threats",     "NMAP threat finding count",  Double(threats.count))
        gauge("novacontrol_goals_total",      "Total OneOnOne goals",       Double(goals.count))
        gauge("novacontrol_goals_completed",  "Completed goals",            Double(goals.filter { $0.status.lowercased() == "completed" }.count))
        gauge("novacontrol_actions_open",     "Open action items",          Double(items.filter { !$0.isCompleted }.count))
        gauge("novacontrol_nova_gateway",     "Nova gateway online (1=up)", nova.gatewayOnline ? 1 : 0)
        gauge("novacontrol_nova_memories",    "Nova memory store count",    Double(nova.memoriesCount))
        gauge("novacontrol_nova_cron_errors", "Nova cron error count",      Double(nova.crons.filter { $0.status == "error" }.count))

        lines.append("")  // trailing newline
        return lines.joined(separator: "\n")
    }

    // MARK: - Response Helpers

    private func encodable<T: Encodable>(_ value: T) -> Any {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = .prettyPrinted
        guard let data = try? encoder.encode(value),
              let json = try? JSONSerialization.jsonObject(with: data) else {
            return ["error": "Encoding failed"]
        }
        return json
    }

    private func sendJSON(connection: NWConnection, status: Int, json: Any) {
        do {
            let data = try JSONSerialization.data(withJSONObject: json, options: .prettyPrinted)
            sendResponse(connection: connection, status: status, body: data, contentType: "application/json")
        } catch {
            sendError(connection: connection, status: 500, message: "JSON serialization error")
        }
    }

    private func sendError(connection: NWConnection, status: Int, message: String) {
        let json: [String: Any] = ["error": message]
        sendJSON(connection: connection, status: status, json: json)
    }

    private func sendResponse(connection: NWConnection, status: Int, body: Data,
                               contentType: String = "application/json",
                               extraHeaders: [String: String] = [:]) {
        let statusText: String
        switch status {
        case 200: statusText = "OK"
        case 207: statusText = "Multi-Status"
        case 304: statusText = "Not Modified"
        case 400: statusText = "Bad Request"
        case 401: statusText = "Unauthorized"
        case 404: statusText = "Not Found"
        case 500: statusText = "Internal Server Error"
        case 503: statusText = "Service Unavailable"
        default:  statusText = "Unknown"
        }

        var headerLines = [
            "HTTP/1.1 \(status) \(statusText)",
            "Content-Type: \(contentType); charset=utf-8",
            "Content-Length: \(body.count)",
            "Connection: close",
        ]
        for (key, value) in extraHeaders {
            headerLines.append("\(key): \(value)")
        }
        headerLines.append(contentsOf: ["", ""])
        let headers = headerLines.joined(separator: "\r\n")

        guard var responseData = headers.data(using: .utf8) else {
            NSLog("[NovaAPIServer] Failed to encode response headers as UTF-8")
            connection.cancel()
            return
        }
        responseData.append(body)

        connection.send(content: responseData, completion: .contentProcessed { error in
            if let error = error {
                NSLog("[NovaAPIServer] Send error: \(error)")
            }
            connection.cancel()
        })
    }
}
