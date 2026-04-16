// WorkflowEngine.swift
// NovaControl — Workflow Automation Engine
//
// Routes action items from OneOnOne → JiraSummary → MailSummary automatically.
// State machine with error handling, retry logic, and Slack notifications.
//
// Written by Jordan Koch.

import Foundation

// MARK: - Workflow Models

struct WorkflowDefinition: Identifiable, Codable {
    let id: String
    let name: String
    let trigger: WorkflowTrigger
    let steps: [WorkflowStep]
    let enabled: Bool

    enum WorkflowTrigger: Codable {
        case newActionItem(priority: String?)   // fires when a new action item appears
        case actionItemCompleted                 // fires when an item is marked done
        case manual                              // POST /api/workflows/{id}/run
    }

    struct WorkflowStep: Codable, Identifiable {
        let id: String
        let name: String
        let type_: StepType
        let config: [String: String]
        let continueOnFailure: Bool

        enum StepType: String, Codable {
            case postToSlack        // POST a message to #nova-notifications (default)
            case createJiraTicket   // Create a Jira issue via JiraSummary API (port 37425)
            case sendEmail          // Send summary email via NovaControl → nova_herd_mail.sh
            case webhook            // POST to arbitrary URL
            case wait               // Pause N seconds before next step
        }
    }
}

struct WorkflowRun: Identifiable, Codable {
    let id: UUID
    let workflowId: String
    let triggeredAt: Date
    let triggerContext: String
    var status: RunStatus
    var stepResults: [StepResult]
    var completedAt: Date?
    var error: String?

    enum RunStatus: String, Codable { case running, completed, failed, retrying }

    struct StepResult: Codable {
        let stepId: String
        let status: String          // "ok", "failed", "skipped"
        let output: String
        let durationMs: Int
    }
}

// MARK: - Workflow Engine

@MainActor
final class WorkflowEngine {
    static let shared = WorkflowEngine()

    private(set) var definitions: [WorkflowDefinition] = []
    private(set) var recentRuns: [WorkflowRun] = []
    private let maxRunHistory = 50
    private let storageURL: URL = {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let dir = support.appendingPathComponent("NovaControl/Workflows", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("definitions.json")
    }()

    private init() {
        loadDefinitions()
        registerBuiltinWorkflows()
    }

    // MARK: - Registration

    private func registerBuiltinWorkflows() {
        // Only register if not already saved
        let ids = definitions.map { $0.id }

        if !ids.contains("action-item-to-slack") {
            let w = WorkflowDefinition(
                id: "action-item-to-slack",
                name: "New Action Item → Slack Alert",
                trigger: .newActionItem(priority: "high"),
                steps: [
                    .init(id: "notify", name: "Post to #nova-notifications", type_: .postToSlack,
                          config: ["channel": "C0ATAF7NZG9",
                                   "messageTemplate": "🎯 New high-priority action item: *{{title}}* assigned to {{assignee}}"],
                          continueOnFailure: false)
                ],
                enabled: true
            )
            definitions.append(w)
        }

        if !ids.contains("action-item-to-jira") {
            let w = WorkflowDefinition(
                id: "action-item-to-jira",
                name: "Completed Action Item → Jira Ticket",
                trigger: .actionItemCompleted,
                steps: [
                    .init(id: "create-ticket", name: "Create Jira Issue", type_: .createJiraTicket,
                          config: ["projectKey": "NOVA",
                                   "issueType": "Task",
                                   "summaryTemplate": "Follow-up: {{title}}",
                                   "targetPort": "37425"],
                          continueOnFailure: true),
                    .init(id: "notify", name: "Notify via Slack", type_: .postToSlack,
                          config: ["channel": "C0ATAF7NZG9",
                                   "messageTemplate": "✅ Action item completed + Jira ticket queued: *{{title}}*"],
                          continueOnFailure: true)
                ],
                enabled: false   // disabled until JiraSummary API is wired up
            )
            definitions.append(w)
        }

        if !ids.contains("daily-action-summary-email") {
            let w = WorkflowDefinition(
                id: "daily-action-summary-email",
                name: "Daily Open Actions Summary Email",
                trigger: .manual,
                steps: [
                    .init(id: "send", name: "Email summary", type_: .sendEmail,
                          config: ["to": "{{OWNER_EMAIL}}",
                                   "subjectTemplate": "Open Action Items — {{date}}",
                                   "bodyTemplate": "You have {{count}} open action items as of {{date}}.",
                                   "scriptPath": "~/.openclaw/scripts/nova_herd_mail.sh"],
                          continueOnFailure: false)
                ],
                enabled: true
            )
            definitions.append(w)
        }

        saveDefinitions()
    }

    // MARK: - Execution

    func run(workflowId: String, context: [String: String] = [:]) async -> WorkflowRun {
        guard let def = definitions.first(where: { $0.id == workflowId }) else {
            var run = WorkflowRun(id: UUID(), workflowId: workflowId, triggeredAt: Date(),
                                  triggerContext: "manual", status: .failed, stepResults: [])
            run.error = "Workflow not found: \(workflowId)"
            run.completedAt = Date()
            return run
        }

        var run = WorkflowRun(id: UUID(), workflowId: workflowId, triggeredAt: Date(),
                              triggerContext: context["trigger"] ?? "manual",
                              status: .running, stepResults: [])

        NSLog("[WorkflowEngine] Starting: \(def.name) (id: \(workflowId))")

        for step in def.steps {
            let start = Date()
            let result = await executeStep(step, context: context, workflowName: def.name)
            let ms = Int(Date().timeIntervalSince(start) * 1000)
            let sr = WorkflowRun.StepResult(stepId: step.id, status: result.ok ? "ok" : "failed",
                                             output: result.output, durationMs: ms)
            run.stepResults.append(sr)

            if !result.ok && !step.continueOnFailure {
                run.status = .failed
                run.error  = "Step '\(step.name)' failed: \(result.output)"
                run.completedAt = Date()
                NSLog("[WorkflowEngine] Step failed (aborting): \(step.name) — \(result.output)")
                storeRun(run)
                return run
            }
        }

        run.status = .completed
        run.completedAt = Date()
        NSLog("[WorkflowEngine] Completed: \(def.name) in \(run.stepResults.map { $0.durationMs }.reduce(0, +))ms")
        storeRun(run)
        return run
    }

    // MARK: - Step Execution

    private struct StepResult { let ok: Bool; let output: String }

    private func executeStep(_ step: WorkflowDefinition.WorkflowStep,
                             context: [String: String],
                             workflowName: String) async -> StepResult {
        switch step.type_ {

        case .postToSlack:
            let token   = step.config["token"] ?? loadSlackTokenFromOpenClaw()
            let channel = step.config["channel"] ?? "C0ATAF7NZG9"
            let message = renderTemplate(step.config["messageTemplate"] ?? "", context: context)
            guard !token.isEmpty else { return StepResult(ok: false, output: "No Slack token") }
            do {
                let payload = ["channel": channel, "text": message]
                var req = URLRequest(url: URL(string: "https://slack.com/api/chat.postMessage")!)
                req.httpMethod = "POST"
                req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                req.setValue("application/json", forHTTPHeaderField: "Content-Type")
                req.httpBody = try JSONSerialization.data(withJSONObject: payload)
                req.timeoutInterval = 10
                let (_, resp) = try await URLSession.shared.data(for: req)
                let ok = (resp as? HTTPURLResponse)?.statusCode == 200
                return StepResult(ok: ok, output: ok ? "Posted to \(channel)" : "Slack API error")
            } catch {
                return StepResult(ok: false, output: "Slack error: \(error.localizedDescription)")
            }

        case .createJiraTicket:
            // Forward to JiraSummary API if running (port 37425)
            let port    = step.config["targetPort"] ?? "37425"
            let summary = renderTemplate(step.config["summaryTemplate"] ?? context["title"] ?? "Task", context: context)
            let project = step.config["projectKey"] ?? "NOVA"
            guard let url = URL(string: "http://127.0.0.1:\(port)/api/issues") else {
                return StepResult(ok: false, output: "Invalid JiraSummary URL")
            }
            do {
                let payload: [String: Any] = ["summary": summary, "project": project,
                                              "issueType": step.config["issueType"] ?? "Task"]
                var req = URLRequest(url: url)
                req.httpMethod = "POST"
                req.setValue("application/json", forHTTPHeaderField: "Content-Type")
                req.httpBody = try JSONSerialization.data(withJSONObject: payload)
                req.timeoutInterval = 10
                let (data, resp) = try await URLSession.shared.data(for: req)
                let ok = (resp as? HTTPURLResponse)?.statusCode == 200
                let output = String(data: data, encoding: .utf8) ?? ""
                return StepResult(ok: ok, output: ok ? "Jira ticket created: \(summary)" : "JiraSummary error: \(output)")
            } catch {
                return StepResult(ok: false, output: "JiraSummary unreachable (port \(port)): \(error.localizedDescription)")
            }

        case .sendEmail:
            let rawScript = step.config["scriptPath"] ?? "~/.openclaw/scripts/nova_herd_mail.sh"
            let script  = rawScript.replacingOccurrences(of: "~", with: NSHomeDirectory())
            let to      = step.config["to"] ?? ""
            guard !to.isEmpty, !to.hasPrefix("{{") else {
                NSLog("[WorkflowEngine] sendEmail: 'to' not configured in workflow step config")
                return StepResult(ok: false, output: "No 'to' address configured")
            }
            let subject = renderTemplate(step.config["subjectTemplate"] ?? "Workflow: \(workflowName)", context: context)
            let body    = renderTemplate(step.config["bodyTemplate"] ?? "Workflow completed.", context: context)
            let result  = await withCheckedContinuation { cont in
                DispatchQueue.global().async {
                    let proc = Process()
                    proc.executableURL = URL(fileURLWithPath: script)
                    proc.arguments = ["send", "--to", to, "--subject", subject, "--body", body]
                    let pipe = Pipe()
                    proc.standardOutput = pipe
                    proc.standardError  = pipe
                    try? proc.run()
                    proc.waitUntilExit()
                    let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                    cont.resume(returning: StepResult(ok: proc.terminationStatus == 0, output: out))
                }
            }
            return result

        case .webhook:
            guard let urlStr = step.config["url"], let url = URL(string: urlStr) else {
                return StepResult(ok: false, output: "No webhook URL configured")
            }
            let payload = context.merging(["workflow": workflowName]) { $1 }
            do {
                var req = URLRequest(url: url)
                req.httpMethod = "POST"
                req.setValue("application/json", forHTTPHeaderField: "Content-Type")
                req.httpBody = try JSONSerialization.data(withJSONObject: payload)
                req.timeoutInterval = 15
                let (_, resp) = try await URLSession.shared.data(for: req)
                let ok = (resp as? HTTPURLResponse)?.statusCode ?? 0 < 400
                return StepResult(ok: ok, output: ok ? "Webhook delivered" : "Webhook HTTP error")
            } catch {
                return StepResult(ok: false, output: "Webhook error: \(error.localizedDescription)")
            }

        case .wait:
            let seconds = Double(step.config["seconds"] ?? "2") ?? 2.0
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            return StepResult(ok: true, output: "Waited \(seconds)s")
        }
    }

    // MARK: - Template Rendering

    private func renderTemplate(_ template: String, context: [String: String]) -> String {
        var result = template
        for (key, value) in context {
            result = result.replacingOccurrences(of: "{{\(key)}}", with: value)
        }
        // Map common aliases: "summary" → "title" if {{title}} still unresolved
        if result.contains("{{title}}"), let summary = context["summary"] {
            result = result.replacingOccurrences(of: "{{title}}", with: summary)
        }
        // Fill {{date}} with today
        let dateFmt = DateFormatter()
        dateFmt.dateStyle = .medium
        result = result.replacingOccurrences(of: "{{date}}", with: dateFmt.string(from: Date()))
        // Remove any remaining unresolved placeholders to prevent literal template spam
        let regex = try? NSRegularExpression(pattern: "\\{\\{[a-zA-Z_]+\\}\\}")
        if let regex = regex {
            result = regex.stringByReplacingMatches(in: result, range: NSRange(result.startIndex..., in: result), withTemplate: "")
        }
        return result.trimmingCharacters(in: .whitespaces)
    }

    // MARK: - Helpers

    private func loadSlackTokenFromOpenClaw() -> String {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath:
                    NSHomeDirectory() + "/.openclaw/openclaw.json")),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let channels = json["channels"] as? [String: Any],
              let slack = channels["slack"] as? [String: Any],
              let token = slack["botToken"] as? String else { return "" }
        return token
    }

    // MARK: - Persistence

    private func loadDefinitions() {
        guard let data = try? Data(contentsOf: storageURL),
              let defs = try? JSONDecoder().decode([WorkflowDefinition].self, from: data) else { return }
        definitions = defs
    }

    private func saveDefinitions() {
        guard let data = try? JSONEncoder().encode(definitions) else { return }
        try? data.write(to: storageURL)
    }

    private func storeRun(_ run: WorkflowRun) {
        recentRuns.insert(run, at: 0)
        if recentRuns.count > maxRunHistory { recentRuns = Array(recentRuns.prefix(maxRunHistory)) }
    }
}
