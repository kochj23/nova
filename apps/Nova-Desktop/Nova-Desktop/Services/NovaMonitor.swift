// NovaMonitor.swift — Nova Desktop
// Main data aggregator. Probes all services every 10s (GitHub every 60s).
// Written by Jordan Koch.

import Foundation
import Network

@MainActor
class NovaMonitor: ObservableObject {
    static let shared = NovaMonitor()

    // MARK: Published State

    @Published var openClaw = OpenClawStatus()
    @Published var aiServices: [MonitoredService] = []
    @Published var apps: [MonitoredService] = []
    @Published var githubRepos: [GitHubRepoStatus] = []
    @Published var novaActivity = NovaActivityStatus()
    @Published var systemStats = SystemStats()
    @Published var ollamaModels: [OllamaModel] = []
    @Published var lastRefresh = Date()
    @Published var isRefreshing = false

    private var refreshTimer: Timer?
    private var githubTimer: Timer?
    private let session: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 3.0
        cfg.timeoutIntervalForResource = 5.0
        return URLSession(configuration: cfg)
    }()

    private init() {
        buildServiceCatalog()
    }

    // MARK: - Build Service Catalog

    private func buildServiceCatalog() {
        aiServices = [
            MonitoredService(
                id: "ollama", name: "Ollama", icon: "cpu.fill", port: 11434,
                startAction: .shell(command: "ollama serve &"),
                stopAction: .shell(command: "pkill ollama")
            ),
            MonitoredService(
                id: "mlxcode", name: "MLX Code", icon: "brain", port: 37422,
                startAction: .launchApp(name: "MLX Code"),
                stopAction: .killApp(name: "MLX Code")
            ),
            MonitoredService(
                id: "openrouter", name: "OpenRouter", icon: "cloud.fill", port: nil,
                openAction: .openURL(url: "https://openrouter.ai")
            ),
            MonitoredService(
                id: "openwebui", name: "Open WebUI", icon: "globe", port: 3000,
                startAction: .openURL(url: "http://127.0.0.1:3000"),
                openAction: .openURL(url: "http://127.0.0.1:3000")
            ),
            MonitoredService(
                id: "tinychat", name: "TinyChat", icon: "bubble.left.fill", port: 5000,
                openAction: .openURL(url: "http://127.0.0.1:5000")
            ),
            MonitoredService(
                id: "swarmui", name: "SwarmUI", icon: "wand.and.sparkles", port: 7801,
                openAction: .openURL(url: "http://127.0.0.1:7801")
            ),
            MonitoredService(
                id: "comfyui", name: "ComfyUI", icon: "photo.on.rectangle.angled", port: 8188,
                openAction: .openURL(url: "http://127.0.0.1:8188")
            ),
            MonitoredService(
                id: "redis", name: "Redis", icon: "cylinder.split.1x2.fill", port: 6379
            ),
            MonitoredService(
                id: "novanextgen", name: "Nova-NextGen", icon: "arrow.triangle.branch", port: 34750,
                openAction: .openURL(url: "http://127.0.0.1:34750")
            ),
        ]

        apps = [
            MonitoredService(
                id: "novacontrol", name: "NovaControl", icon: "antenna.radiowaves.left.and.right", port: 37400,
                startAction: .launchApp(name: "NovaControl"),
                stopAction: .killApp(name: "NovaControl")
            ),
            MonitoredService(
                id: "nmapscanner", name: "NMAPScanner", icon: "shield.lefthalf.filled", port: 37423,
                startAction: .launchApp(name: "NMAPScanner"),
                stopAction: .killApp(name: "NMAPScanner")
            ),
            MonitoredService(
                id: "oneonone", name: "OneOnOne", icon: "person.2.fill", port: 37421,
                startAction: .launchApp(name: "OneOnOne"),
                stopAction: .killApp(name: "OneOnOne")
            ),
            MonitoredService(
                id: "rsyncgui", name: "RsyncGUI", icon: "arrow.triangle.2.circlepath", port: 37424,
                startAction: .launchApp(name: "RsyncGUI"),
                stopAction: .killApp(name: "RsyncGUI")
            ),
            MonitoredService(
                id: "jirasummary", name: "JiraSummary", icon: "checkmark.seal.fill", port: 37425,
                startAction: .launchApp(name: "JiraSummary"),
                stopAction: .killApp(name: "JiraSummary")
            ),
            MonitoredService(
                id: "mailsummary", name: "Mail Summary", icon: "envelope.fill", port: 37430,
                startAction: .launchApp(name: "Mail Summary"),
                stopAction: .killApp(name: "Mail Summary")
            ),
        ]
    }

    // MARK: - Start / Stop Monitoring

    func startMonitoring() {
        Task { await refresh() }
        Task { await refreshGitHub() }

        refreshTimer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in await self?.refresh() }
        }
        githubTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in await self?.refreshGitHub() }
        }
    }

    func stopMonitoring() {
        refreshTimer?.invalidate(); refreshTimer = nil
        githubTimer?.invalidate();  githubTimer = nil
    }

    // MARK: - Full Refresh

    func refresh() async {
        isRefreshing = true
        defer { isRefreshing = false; lastRefresh = Date() }

        async let oc = probeOpenClaw()
        async let ai = probeAIServices()
        async let ap = probeApps()
        async let ac = probeNovaActivity()
        async let sys = probeSystemStats()

        let (ocResult, aiResult, apResult, acResult, sysResult) = await (oc, ai, ap, ac, sys)
        openClaw     = ocResult
        aiServices   = aiResult
        apps         = apResult
        novaActivity = acResult
        systemStats  = sysResult
    }

    // MARK: - OpenClaw

    private func probeOpenClaw() async -> OpenClawStatus {
        var status = OpenClawStatus()

        // Gateway — /health returns {"ok":true,"status":"live"}
        if let (data, _) = try? await session.data(from: URL(string: "http://127.0.0.1:18789/health")!),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           json["ok"] as? Bool == true {
            status.gatewayOnline = true
            status.gatewayVersion = json["status"] as? String ?? "live"
        }
        // Enrich with openclaw CLI for session/model data
        if status.gatewayOnline {
            let cliOut = shell("openclaw status 2>/dev/null")
            if let sessLine = cliOut.components(separatedBy: "\n").first(where: { $0.contains("sessions") }),
               let numStr = sessLine.components(separatedBy: " ").first(where: { Int($0) != nil }),
               let n = Int(numStr) { status.activeSessions = n }
            if let modelLine = cliOut.components(separatedBy: "\n").first(where: { $0.contains("default") && $0.contains("/") }) {
                let parts = modelLine.components(separatedBy: " ")
                if let modelIdx = parts.firstIndex(where: { $0.contains("/") }) {
                    status.currentModel = parts[modelIdx]
                }
            }
        }

        // Memory server
        if let (data, _) = try? await session.data(from: URL(string: "http://127.0.0.1:18790/health")!),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            status.memoryServerOnline    = true
            status.memoriesCount         = json["total_chunks"] as? Int ?? json["count"] as? Int ?? 0
            status.memoryBackend         = json["backend"] as? String ?? "postgresql+pgvector"
            status.memoryQueueDepth      = json["queue_length"] as? Int ?? 0
        }
        // Memory /search endpoint
        if let (_, resp) = try? await session.data(from: URL(string: "http://127.0.0.1:18790/search?q=test&n=1")!),
           (resp as? HTTPURLResponse)?.statusCode ?? 0 < 500 {
            status.memorySearchEndpoint = true
        }
        // Redis queue
        if let (_, resp) = try? await session.data(from: URL(string: "http://127.0.0.1:6379")!),
           (resp as? HTTPURLResponse) != nil {
            status.redisOnline = true
        } else {
            // TCP-level check for Redis (it doesn't speak HTTP)
            let redisCheck = shell("redis-cli ping 2>/dev/null")
            status.redisOnline = redisCheck.trimmingCharacters(in: .whitespacesAndNewlines) == "PONG"
        }

        // Cron jobs via openclaw CLI
        let cronOutput = shell("openclaw cron list --json 2>/dev/null")
        if let data = cronOutput.data(using: .utf8),
           let arr = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] {
            status.cronJobs = arr.compactMap { parseCronJob($0) }
        }

        // Slack channel connectivity from NovaControl
        if let (data, _) = try? await session.data(from: URL(string: "http://127.0.0.1:37400/api/nova/status")!),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            status.slackConnected  = json["gatewayOnline"] as? Bool ?? false
            status.activeSessions  = json["activeSessions"] as? Int ?? status.activeSessions
            if let model = json["currentModel"] as? String { status.currentModel = model }
        }

        return status
    }

    private func parseCronJob(_ d: [String: Any]) -> CronJobStatus? {
        guard let id = d["id"] as? String else { return nil }
        let schedule: String
        if let s = (d["schedule"] as? [String: Any]) {
            schedule = s["expr"] as? String ?? s["everyMs"].map { "every \($0)ms" } ?? "—"
        } else { schedule = "—" }
        return CronJobStatus(
            id: id,
            name: d["name"] as? String ?? id,
            schedule: schedule,
            state: (d["state"] as? [String: Any])?["lastStatus"] as? String ?? "unknown",
            lastRun: formatRelativeTime((d["state"] as? [String: Any])?["lastRunAtMs"] as? Double),
            nextRun: formatRelativeTime((d["state"] as? [String: Any])?["nextRunAtMs"] as? Double),
            consecutiveErrors: (d["state"] as? [String: Any])?["consecutiveErrors"] as? Int ?? 0,
            target: d["sessionTarget"] as? String ?? "—"
        )
    }

    // MARK: - AI Services

    private func probeAIServices() async -> [MonitoredService] {
        var results = aiServices
        await withTaskGroup(of: (Int, MonitoredService).self) { group in
            for (i, svc) in results.enumerated() {
                group.addTask { [weak self] in
                    guard let self else { return (i, svc) }
                    return (i, await self.probeAIService(index: i, service: svc))
                }
            }
            for await (i, updated) in group { results[i] = updated }
        }
        return results
    }

    private func probeAIService(index: Int, service: MonitoredService) async -> MonitoredService {
        var svc = service
        let start = Date()

        switch svc.id {
        case "ollama":
            if let (data, _) = try? await session.data(from: URL(string: "http://127.0.0.1:11434/api/tags")!) {
                svc.state = .online
                svc.latencyMs = Int(Date().timeIntervalSince(start) * 1000)
                if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let models = json["models"] as? [[String: Any]] {
                    let names = models.prefix(3).compactMap { $0["name"] as? String }.map { shortModelName($0) }
                    svc.detail = names.isEmpty ? "no models" : names.joined(separator: ", ")
                    let parsed = models.map { m -> OllamaModel in
                        OllamaModel(
                            name: m["name"] as? String ?? "?",
                            size: formatBytes(m["size"] as? Int64 ?? 0),
                            modified: m["modified_at"] as? String ?? ""
                        )
                    }
                    await MainActor.run { self.ollamaModels = parsed }
                }
            } else { svc.state = .offline; svc.detail = "not running" }

        case "mlxcode":
            if let (data, _) = try? await session.data(from: URL(string: "http://127.0.0.1:37422/api/status")!) {
                svc.state = .online
                svc.latencyMs = Int(Date().timeIntervalSince(start) * 1000)
                if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    let model = (json["currentModel"] as? String).map { shortModelName($0) } ?? ""
                    let tps = json["tokensPerSecond"] as? Double ?? 0
                    svc.detail = model.isEmpty ? "loaded" : "\(model)\(tps > 0 ? " · \(Int(tps)) t/s" : "")"
                }
            } else { svc.state = .offline; svc.detail = "not running" }

        case "openrouter":
            // Check connectivity via a lightweight ping
            if let url = URL(string: "https://openrouter.ai/api/v1/models"),
               let (_, resp) = try? await session.data(from: url),
               (resp as? HTTPURLResponse)?.statusCode == 200 {
                svc.state = .online
                svc.latencyMs = Int(Date().timeIntervalSince(start) * 1000)
                svc.detail = "API reachable"
            } else { svc.state = .degraded; svc.detail = "unreachable" }

        case "openwebui":
            for port in [3000, 8080] {
                if let (_, resp) = try? await session.data(from: URL(string: "http://127.0.0.1:\(port)/api/version")!),
                   (resp as? HTTPURLResponse)?.statusCode ?? 0 < 500 {
                    svc.state = .online; svc.latencyMs = Int(Date().timeIntervalSince(start) * 1000)
                    svc.detail = "port \(port)"; break
                }
            }
            if svc.state == .unknown { svc.state = .offline; svc.detail = "not running" }

        case "tinychat":
            for port in [5000, 8080, 8000] {
                if let (_, resp) = try? await session.data(from: URL(string: "http://127.0.0.1:\(port)/v1/models")!),
                   (resp as? HTTPURLResponse)?.statusCode ?? 0 < 500 {
                    svc.state = .online; svc.latencyMs = Int(Date().timeIntervalSince(start) * 1000)
                    svc.detail = "port \(port)"; break
                }
            }
            if svc.state == .unknown { svc.state = .offline; svc.detail = "not running" }

        case "swarmui":
            if let (_, resp) = try? await session.data(from: URL(string: "http://127.0.0.1:7801/API/GetNewSession")!),
               let http = resp as? HTTPURLResponse, http.statusCode < 500 {
                svc.state = .online; svc.latencyMs = Int(Date().timeIntervalSince(start) * 1000)
                svc.detail = "ready"
            } else { svc.state = .offline; svc.detail = "not running" }

        case "comfyui":
            if let (data, _) = try? await session.data(from: URL(string: "http://127.0.0.1:8188/system_stats")!) {
                svc.state = .online; svc.latencyMs = Int(Date().timeIntervalSince(start) * 1000)
                if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let sys = json["system"] as? [String: Any],
                   let vram = sys["vram_total"] as? Int64 {
                    svc.detail = "VRAM: \(formatBytes(vram))"
                } else { svc.detail = "running" }
            } else { svc.state = .offline; svc.detail = "not running" }

        case "redis":
            let ping = shell("redis-cli ping 2>/dev/null").trimmingCharacters(in: .whitespacesAndNewlines)
            if ping == "PONG" {
                svc.state = .online
                let info = shell("redis-cli info server 2>/dev/null")
                if let versionLine = info.components(separatedBy: "\n").first(where: { $0.contains("redis_version") }) {
                    svc.detail = "v" + (versionLine.components(separatedBy: ":").last?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "?")
                } else { svc.detail = "online" }
            } else { svc.state = .offline; svc.detail = "not running" }

        case "novanextgen":
            if let (data, _) = try? await session.data(from: URL(string: "http://127.0.0.1:34750/api/ai/backends")!) {
                svc.state = .online
                svc.latencyMs = Int(Date().timeIntervalSince(start) * 1000)
                if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let backends = json["backends"] as? [[String: Any]] {
                    let activeCount = backends.filter { $0["available"] as? Bool == true }.count
                    svc.detail = "\(activeCount)/\(backends.count) backends"
                } else { svc.detail = "online" }
            } else { svc.state = .offline; svc.detail = "not running" }

        default:
            if let port = svc.port,
               let (_, resp) = try? await session.data(from: URL(string: "http://127.0.0.1:\(port)/")!),
               (resp as? HTTPURLResponse)?.statusCode ?? 0 < 500 {
                svc.state = .online; svc.latencyMs = Int(Date().timeIntervalSince(start) * 1000)
            } else { svc.state = .offline }
        }

        svc.lastChecked = Date()
        return svc
    }

    // MARK: - Apps

    private func probeApps() async -> [MonitoredService] {
        var results = apps
        await withTaskGroup(of: (Int, MonitoredService).self) { group in
            for (i, app) in results.enumerated() {
                group.addTask { [weak self] in
                    guard let self, let port = app.port else {
                        var s = app; s.state = .unknown; return (i, s)
                    }
                    return (i, await self.probeApp(index: i, service: app, port: port))
                }
            }
            for await (i, updated) in group { results[i] = updated }
        }
        return results
    }

    private func probeApp(index: Int, service: MonitoredService, port: Int) async -> MonitoredService {
        var svc = service
        let start = Date()
        let pingURL = URL(string: "http://127.0.0.1:\(port)/api/ping")
            ?? URL(string: "http://127.0.0.1:\(port)/api/status")!
        if let (data, resp) = try? await session.data(from: pingURL),
           let http = resp as? HTTPURLResponse, http.statusCode < 500 {
            svc.state = .online
            svc.latencyMs = Int(Date().timeIntervalSince(start) * 1000)
            if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let ver = json["version"] as? String { svc.detail = "v\(ver)" }
        } else { svc.state = .offline; svc.detail = "port \(port)" }
        svc.lastChecked = Date()
        return svc
    }

    // MARK: - Nova Activity

    private func probeNovaActivity() async -> NovaActivityStatus {
        var activity = NovaActivityStatus()

        // Query NovaControl's Nova status endpoint
        if let (data, _) = try? await session.data(from: URL(string: "http://127.0.0.1:37400/api/nova/status")!),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            activity.slackOnline    = json["gatewayOnline"] as? Bool ?? false
            activity.activeSessions = json["activeSessions"] as? Int ?? 0
            activity.cronErrorCount = (json["crons"] as? [[String: Any]] ?? [])
                .filter { ($0["status"] as? String) == "error" }.count
        }

        // Email unread count via herd-mail check
        let emailCheck = shell("~/.openclaw/scripts/nova_herd_mail.sh check 2>/dev/null")
        if let data = emailCheck.data(using: .utf8),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let count = json["unread_count"] as? Int {
            activity.emailUnreadCount = count
            activity.lastEmailCheck   = Date()
        }

        // Last Slack message time — check channel history
        if let (data, _) = try? await session.data(from: URL(string: "https://slack.com/api/conversations.history?channel=C0AMNQ5GX70&limit=1")!.withSlackToken()),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let msgs = json["messages"] as? [[String: Any]],
           let ts = msgs.first?["ts"] as? String,
           let tsDouble = Double(ts) {
            activity.lastSlackMessageDate = Date(timeIntervalSince1970: tsDouble)
        }

        return activity
    }

    // MARK: - System Stats

    private func probeSystemStats() async -> SystemStats {
        if let (data, _) = try? await session.data(from: URL(string: "http://127.0.0.1:37400/api/system/stats")!),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            return SystemStats(
                cpuPercent:    (json["cpuUser"] as? Double ?? 0) + (json["cpuSystem"] as? Double ?? 0),
                ramPercent:    ((json["memUsedGB"] as? Double ?? 0) / max(json["memTotalGB"] as? Double ?? 1, 1)) * 100,
                diskReadMBs:   json["diskReadMBs"] as? Double ?? 0,
                diskWriteMBs:  json["diskWriteMBs"] as? Double ?? 0,
                uptimeSeconds: json["uptime"] as? TimeInterval ?? 0
            )
        }
        return systemStats   // keep last known
    }

    // MARK: - GitHub

    func refreshGitHub() async {
        let repos = ["kochj23/nova", "kochj23/Nova-Desktop", "kochj23/NovaControl", "kochj23/MLXCode", "kochj23/NMAPScanner",
                     "kochj23/RsyncGUI", "kochj23/JiraSummary"]
        var results: [GitHubRepoStatus] = []
        await withTaskGroup(of: GitHubRepoStatus?.self) { group in
            for repo in repos {
                group.addTask { [weak self] in await self?.probeGitHubRepo(repo) }
            }
            for await r in group { if let r { results.append(r) } }
        }
        results.sort { $0.name < $1.name }
        githubRepos = results
    }

    private func probeGitHubRepo(_ fullName: String) async -> GitHubRepoStatus? {
        var headers: [String: String] = ["Accept": "application/vnd.github.v3+json"]
        if let token = loadGitHubToken() { headers["Authorization"] = "Bearer \(token)" }

        guard let url = URL(string: "https://api.github.com/repos/\(fullName)") else { return nil }
        var req = URLRequest(url: url)
        headers.forEach { req.setValue($1, forHTTPHeaderField: $0) }
        guard let (data, resp) = try? await session.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }

        var repo = GitHubRepoStatus(
            name: json["name"] as? String ?? fullName,
            fullName: fullName,
            description: json["description"] as? String ?? "",
            openIssues: json["open_issues_count"] as? Int ?? 0,
            stars: json["stargazers_count"] as? Int ?? 0,
            isPrivate: json["private"] as? Bool ?? false,
            defaultBranch: json["default_branch"] as? String ?? "main"
        )

        // Last commit
        if let commitsURL = URL(string: "https://api.github.com/repos/\(fullName)/commits?per_page=1") {
            var cReq = URLRequest(url: commitsURL)
            headers.forEach { cReq.setValue($1, forHTTPHeaderField: $0) }
            if let (cData, _) = try? await session.data(for: cReq),
               let commits = try? JSONSerialization.jsonObject(with: cData) as? [[String: Any]],
               let first = commits.first,
               let commit = first["commit"] as? [String: Any],
               let message = (commit["message"] as? String)?.components(separatedBy: "\n").first {
                repo.lastCommitMessage = message
                if let dateStr = (commit["author"] as? [String: Any])?["date"] as? String {
                    repo.lastCommitDate = ISO8601DateFormatter().date(from: dateStr)
                }
            }
        }

        // Open PRs count
        if let prsURL = URL(string: "https://api.github.com/repos/\(fullName)/pulls?state=open&per_page=1") {
            var pReq = URLRequest(url: prsURL)
            headers.forEach { pReq.setValue($1, forHTTPHeaderField: $0) }
            if let (_, pResp) = try? await session.data(for: pReq),
               let http = pResp as? HTTPURLResponse,
               let linkHeader = http.value(forHTTPHeaderField: "Link") {
                // Parse last page number from Link header for count
                repo.openPRs = parsePRCount(from: linkHeader)
            }
        }

        return repo
    }

    // MARK: - Helpers

    @discardableResult
    func shell(_ command: String) -> String {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = ["-c", command]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = Pipe()
        try? proc.run()
        proc.waitUntilExit()
        return String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    }

    private func loadGitHubToken() -> String? {
        let result = shell("security find-generic-password -a kochj23 -s github-token -w 2>/dev/null").trimmingCharacters(in: .whitespacesAndNewlines)
        return result.isEmpty ? nil : result
    }

    private func loadSlackToken() -> String? {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: NSHomeDirectory() + "/.openclaw/openclaw.json")),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let channels = json["channels"] as? [String: Any],
              let slack = channels["slack"] as? [String: Any],
              let token = slack["botToken"] as? String else { return nil }
        return token
    }

    private func shortModelName(_ name: String) -> String {
        let s = name.replacingOccurrences(of: "openrouter/", with: "")
                    .replacingOccurrences(of: "anthropic/", with: "")
                    .replacingOccurrences(of: "deepseek/", with: "ds/")
        return s.count > 18 ? String(s.suffix(18)) : s
    }

    private func formatBytes(_ bytes: Int64) -> String {
        let gb = Double(bytes) / 1_073_741_824
        if gb >= 1 { return String(format: "%.1fGB", gb) }
        let mb = Double(bytes) / 1_048_576
        return String(format: "%.0fMB", mb)
    }

    private func formatRelativeTime(_ ms: Double?) -> String {
        guard let ms else { return "—" }
        let date = Date(timeIntervalSince1970: ms / 1000)
        let diff = Date().timeIntervalSince(date)
        if diff < 60 { return "just now" }
        if diff < 3600 { return "\(Int(diff / 60))m ago" }
        if diff < 86400 { return "\(Int(diff / 3600))h ago" }
        return "\(Int(diff / 86400))d ago"
    }

    private func parsePRCount(from linkHeader: String) -> Int {
        // Link: <url?page=N>; rel="last" — extract N
        guard let lastRange = linkHeader.range(of: "page=(\\d+)>; rel=\"last\"", options: .regularExpression),
              let pageStr = linkHeader[lastRange].components(separatedBy: "page=").last?.components(separatedBy: ">").first,
              let count = Int(pageStr) else { return 0 }
        return count
    }
}

// MARK: - URL Extension for Slack Token

private extension URL {
    func withSlackToken() -> URL {
        guard var comps = URLComponents(url: self, resolvingAgainstBaseURL: false) else { return self }
        // Token is set in headers, not URL — just return self
        return self
    }
}
