// ServiceModels.swift — Nova Desktop
// Written by Jordan Koch.

import Foundation

// MARK: - Service State

enum ServiceState: String, Equatable {
    case online   = "Online"
    case degraded = "Degraded"
    case offline  = "Offline"
    case unknown  = "Unknown"
}

// MARK: - Monitored Service

struct MonitoredService: Identifiable {
    let id: String
    let name: String
    let icon: String
    let port: Int?
    var state: ServiceState = .unknown
    var detail: String = ""         // version, model name, tokens/sec, etc.
    var latencyMs: Int? = nil
    var startAction: ServiceAction? = nil
    var stopAction: ServiceAction?  = nil
    var openAction: ServiceAction?  = nil   // "Open in browser" etc.
    var lastChecked: Date = Date()
}

enum ServiceAction {
    case launchApp(name: String)
    case killApp(name: String)
    case shell(command: String)
    case openURL(url: String)
}

// MARK: - OpenClaw Status

struct OpenClawStatus {
    var gatewayOnline: Bool = false
    var gatewayVersion: String = "—"
    var activeSessions: Int = 0
    var memoryServerOnline: Bool = false
    var memoriesCount: Int = 0
    var slackConnected: Bool = false
    var currentModel: String = "—"
    var uptimeSeconds: Int = 0
    var cronJobs: [CronJobStatus] = []
    var lastUpdated: Date = Date()
    var memoryBackend: String = "postgresql+pgvector"
    var redisOnline: Bool = false
    var memoryQueueDepth: Int = 0
    var memorySearchEndpoint: Bool = false
}

struct CronJobStatus: Identifiable {
    let id: String
    var name: String
    var schedule: String
    var state: String          // "ok", "error", "running", "skipped"
    var lastRun: String
    var nextRun: String
    var consecutiveErrors: Int
    var target: String

    var stateColor: String {
        switch state {
        case "ok":      return "green"
        case "error":   return "red"
        case "running": return "cyan"
        default:        return "yellow"
        }
    }
}

// MARK: - GitHub Repo Status

struct GitHubRepoStatus: Identifiable {
    var id: String { fullName }
    var name: String
    var fullName: String
    var description: String = ""
    var lastCommitMessage: String = "—"
    var lastCommitDate: Date? = nil
    var openIssues: Int = 0
    var openPRs: Int = 0
    var stars: Int = 0
    var isPrivate: Bool = false
    var defaultBranch: String = "main"
}

// MARK: - Nova Activity

struct NovaActivityStatus {
    var slackOnline: Bool = false
    var lastSlackMessageDate: Date? = nil
    var emailUnreadCount: Int = 0
    var lastEmailCheck: Date? = nil
    var lastCronRun: Date? = nil
    var cronErrorCount: Int = 0
    var activeSessions: Int = 0
}

// MARK: - System Stats (mini — reuse NovaControl data)

struct SystemStats {
    var cpuPercent: Double = 0
    var ramPercent: Double = 0
    var diskReadMBs: Double = 0
    var diskWriteMBs: Double = 0
    var uptimeSeconds: TimeInterval = 0
}

// MARK: - Ollama Model

struct OllamaModel: Identifiable {
    var id: String { name }
    var name: String
    var size: String
    var modified: String
}
