// NovaControl — Alerting Engine
// Written by Jordan Koch
// Configurable alert rules evaluated on each refresh cycle.
// Supports Slack notifications + macOS Notification Center.
// Escalation after repeated firing.

import Foundation
import UserNotifications

// MARK: - Alert Rule Model

struct AlertRule: Identifiable, Codable {
    let id: String
    let name: String
    let condition: AlertCondition
    let channels: [AlertChannel]
    let enabled: Bool
    let cooldownSeconds: Int        // Minimum time between firings
    let escalateAfter: Int          // Fire count before escalation (0 = never)
    let escalationChannel: AlertChannel?

    enum AlertCondition: Codable {
        case cpuAbove(percent: Double)
        case memoryAbove(percent: Double)
        case serviceOffline(serviceId: String)
        case cronError(minErrors: Int)
        case threatDetected(minSeverity: String)
        case diskIOAbove(writeMBs: Double)
        case customMetric(name: String, threshold: Double, comparison: String)  // "gt", "lt", "eq"
    }

    enum AlertChannel: String, Codable {
        case slack
        case macOSNotification
    }
}

// MARK: - Alert Event

struct AlertEvent: Identifiable, Codable {
    let id: UUID
    let ruleId: String
    let ruleName: String
    let firedAt: Date
    let message: String
    let wasEscalated: Bool
}

// MARK: - Alerting Engine

@MainActor
final class AlertingEngine {
    static let shared = AlertingEngine()

    private(set) var rules: [AlertRule] = []
    private(set) var recentEvents: [AlertEvent] = []
    private let maxEventHistory = 100

    /// Tracks consecutive fire counts per rule for escalation
    private var fireCounters: [String: Int] = [:]
    /// Last fire time per rule for cooldown enforcement
    private var lastFired: [String: Date] = [:]

    private let storageURL: URL = {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let dir = support.appendingPathComponent("NovaControl/Alerts", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("rules.json")
    }()

    private init() {
        loadRules()
        registerDefaultRules()
        requestNotificationPermission()
    }

    // MARK: - Default Rules

    private func registerDefaultRules() {
        let ids = rules.map { $0.id }

        if !ids.contains("cpu-critical") {
            rules.append(AlertRule(
                id: "cpu-critical", name: "CPU Critical (>90%)",
                condition: .cpuAbove(percent: 90),
                channels: [.macOSNotification, .slack],
                enabled: true, cooldownSeconds: 300, escalateAfter: 3,
                escalationChannel: .slack
            ))
        }

        if !ids.contains("memory-high") {
            rules.append(AlertRule(
                id: "memory-high", name: "Memory High (>85%)",
                condition: .memoryAbove(percent: 85),
                channels: [.macOSNotification],
                enabled: true, cooldownSeconds: 600, escalateAfter: 5,
                escalationChannel: .slack
            ))
        }

        if !ids.contains("nova-offline") {
            rules.append(AlertRule(
                id: "nova-offline", name: "Nova Gateway Offline",
                condition: .serviceOffline(serviceId: "nova"),
                channels: [.macOSNotification, .slack],
                enabled: true, cooldownSeconds: 120, escalateAfter: 2,
                escalationChannel: .slack
            ))
        }

        if !ids.contains("cron-errors") {
            rules.append(AlertRule(
                id: "cron-errors", name: "Cron Errors Detected",
                condition: .cronError(minErrors: 2),
                channels: [.macOSNotification],
                enabled: true, cooldownSeconds: 900, escalateAfter: 0,
                escalationChannel: nil
            ))
        }

        if !ids.contains("threat-high") {
            rules.append(AlertRule(
                id: "threat-high", name: "High Severity Threat",
                condition: .threatDetected(minSeverity: "high"),
                channels: [.macOSNotification, .slack],
                enabled: true, cooldownSeconds: 60, escalateAfter: 1,
                escalationChannel: .slack
            ))
        }

        saveRules()
    }

    // MARK: - Evaluation (called on each DataManager refresh)

    func evaluate(stats: SystemStats?, novaStatus: NovaStatus?, threats: [ThreatFinding],
                  services: [ServiceInfo]) {
        for rule in rules where rule.enabled {
            let shouldFire = checkCondition(rule.condition, stats: stats,
                                            novaStatus: novaStatus, threats: threats,
                                            services: services)

            if shouldFire {
                // Cooldown check
                if let last = lastFired[rule.id],
                   Date().timeIntervalSince(last) < Double(rule.cooldownSeconds) {
                    continue
                }

                fireCounters[rule.id, default: 0] += 1
                lastFired[rule.id] = Date()

                let count = fireCounters[rule.id] ?? 1
                let escalated = rule.escalateAfter > 0 && count >= rule.escalateAfter
                let message = buildMessage(for: rule, fireCount: count, escalated: escalated)

                let event = AlertEvent(
                    id: UUID(), ruleId: rule.id, ruleName: rule.name,
                    firedAt: Date(), message: message, wasEscalated: escalated
                )
                recordEvent(event)

                // Dispatch to channels
                var channels = rule.channels
                if escalated, let esc = rule.escalationChannel, !channels.contains(esc) {
                    channels.append(esc)
                }
                for channel in channels {
                    dispatch(message: message, to: channel, escalated: escalated)
                }
            } else {
                // Reset counter on recovery
                fireCounters[rule.id] = 0
            }
        }
    }

    // MARK: - Condition Checking

    private func checkCondition(_ condition: AlertRule.AlertCondition,
                                stats: SystemStats?,
                                novaStatus: NovaStatus?,
                                threats: [ThreatFinding],
                                services: [ServiceInfo]) -> Bool {
        switch condition {
        case .cpuAbove(let threshold):
            guard let stats = stats else { return false }
            return (stats.cpuUser + stats.cpuSystem) > threshold

        case .memoryAbove(let threshold):
            guard let stats = stats else { return false }
            guard stats.memTotalGB > 0 else { return false }
            let percent = (stats.memUsedGB / stats.memTotalGB) * 100.0
            return percent > threshold

        case .serviceOffline(let serviceId):
            if serviceId == "nova" {
                return novaStatus?.gatewayOnline == false
            }
            return services.first(where: { $0.id == serviceId })?.status == .offline

        case .cronError(let minErrors):
            guard let nova = novaStatus else { return false }
            let errorCount = nova.crons.filter { $0.status == "error" }.count
            return errorCount >= minErrors

        case .threatDetected(let minSeverity):
            let severityOrder = ["low": 0, "medium": 1, "high": 2, "critical": 3]
            let minLevel = severityOrder[minSeverity.lowercased()] ?? 1
            return threats.contains { severityOrder[$0.severity.lowercased()] ?? 0 >= minLevel }

        case .diskIOAbove(let threshold):
            guard let stats = stats else { return false }
            return stats.diskWriteMBs > threshold

        case .customMetric:
            // Custom metrics can be evaluated via external POST
            return false
        }
    }

    // MARK: - Message Building

    private func buildMessage(for rule: AlertRule, fireCount: Int, escalated: Bool) -> String {
        var msg = "[NovaControl Alert] \(rule.name)"
        if escalated {
            msg += " [ESCALATED - fired \(fireCount)x]"
        }
        return msg
    }

    // MARK: - Dispatching

    private func dispatch(message: String, to channel: AlertRule.AlertChannel, escalated: Bool) {
        switch channel {
        case .macOSNotification:
            sendMacNotification(message: message, escalated: escalated)

        case .slack:
            Task {
                await sendSlackAlert(message: message, escalated: escalated)
            }
        }
    }

    private func sendMacNotification(message: String, escalated: Bool) {
        let content = UNMutableNotificationContent()
        content.title = escalated ? "NovaControl ALERT (Escalated)" : "NovaControl Alert"
        content.body = message
        content.sound = escalated ? .defaultCritical : .default

        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { error in
            if let error = error {
                NSLog("[AlertingEngine] Notification error: \(error.localizedDescription)")
            }
        }
    }

    private func sendSlackAlert(message: String, escalated: Bool) async {
        // Load Slack token from openclaw config
        guard let data = try? Data(contentsOf: URL(fileURLWithPath:
                    NSHomeDirectory() + "/.openclaw/openclaw.json")),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let channels = json["channels"] as? [String: Any],
              let slack = channels["slack"] as? [String: Any],
              let token = slack["botToken"] as? String, !token.isEmpty else {
            NSLog("[AlertingEngine] No Slack token available for alert dispatch")
            return
        }

        let channel = "C0ATAF7NZG9" // #nova-notifications
        let emoji = escalated ? ":rotating_light:" : ":warning:"
        let payload: [String: Any] = ["channel": channel, "text": "\(emoji) \(message)"]

        guard let url = URL(string: "https://slack.com/api/chat.postMessage") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        request.timeoutInterval = 10

        _ = try? await URLSession.shared.data(for: request)
    }

    // MARK: - Notification Permission

    private func requestNotificationPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, error in
            if let error = error {
                NSLog("[AlertingEngine] Notification auth error: \(error.localizedDescription)")
            }
            if !granted {
                NSLog("[AlertingEngine] Notification permission denied")
            }
        }
    }

    // MARK: - Event Storage

    private func recordEvent(_ event: AlertEvent) {
        recentEvents.insert(event, at: 0)
        if recentEvents.count > maxEventHistory {
            recentEvents = Array(recentEvents.prefix(maxEventHistory))
        }
    }

    // MARK: - Persistence

    private func loadRules() {
        guard let data = try? Data(contentsOf: storageURL),
              let loaded = try? JSONDecoder().decode([AlertRule].self, from: data) else { return }
        rules = loaded
    }

    private func saveRules() {
        guard let data = try? JSONEncoder().encode(rules) else { return }
        try? data.write(to: storageURL)
    }

    // MARK: - API Helpers

    func rulesAsJSON() -> [[String: Any]] {
        return rules.map { rule in
            [
                "id": rule.id,
                "name": rule.name,
                "enabled": rule.enabled,
                "cooldownSeconds": rule.cooldownSeconds,
                "escalateAfter": rule.escalateAfter,
                "channels": rule.channels.map { $0.rawValue }
            ]
        }
    }

    func eventsAsJSON(limit: Int = 50) -> [[String: Any]] {
        let iso = ISO8601DateFormatter()
        return Array(recentEvents.prefix(limit)).map { event in
            [
                "id": event.id.uuidString,
                "ruleId": event.ruleId,
                "ruleName": event.ruleName,
                "firedAt": iso.string(from: event.firedAt),
                "message": event.message,
                "wasEscalated": event.wasEscalated
            ]
        }
    }
}
