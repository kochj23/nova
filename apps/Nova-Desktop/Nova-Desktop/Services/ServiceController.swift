// ServiceController.swift — Nova Desktop
// Start, stop, and restart any monitored service.
// Written by Jordan Koch.

import Foundation
import AppKit

@MainActor
final class ServiceController {
    static let shared = ServiceController()
    private init() {}

    // MARK: - Public Interface

    func perform(_ action: ServiceAction, serviceId: String) {
        switch action {
        case .launchApp(let name):    launchApp(named: name)
        case .killApp(let name):      killApp(named: name)
        case .shell(let command):     shell(command)
        case .openURL(let urlString): openURL(urlString)
        }
        NSLog("[ServiceController] Performed action for \(serviceId): \(action)")
    }

    func restart(service: MonitoredService) {
        if let stop = service.stopAction  { perform(stop,  serviceId: service.id) }
        // Brief pause then start
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            if let start = service.startAction { self.perform(start, serviceId: service.id) }
        }
    }

    // MARK: - Actions

    private func launchApp(named name: String) {
        NSWorkspace.shared.openApplication(at: appURL(name) ?? URL(fileURLWithPath: "/Applications/\(name).app"),
                                           configuration: NSWorkspace.OpenConfiguration())
        NSLog("[ServiceController] Launched: \(name)")
    }

    private func killApp(named name: String) {
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: bundleId(for: name))
        running.forEach { $0.terminate() }
        if running.isEmpty {
            shell("pkill -x \"\(name)\"")
        }
        NSLog("[ServiceController] Killed: \(name)")
    }

    @discardableResult
    func shell(_ command: String) -> String {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = ["-c", command]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError  = Pipe()
        try? proc.run()
        proc.waitUntilExit()
        let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        NSLog("[ServiceController] shell(\(command.prefix(60))) → exit \(proc.terminationStatus)")
        return output
    }

    private func openURL(_ urlString: String) {
        guard let url = URL(string: urlString) else { return }
        NSWorkspace.shared.open(url)
    }

    // MARK: - Well-Known Actions

    func restartOpenClawGateway() {
        let uid = shell("id -u").trimmingCharacters(in: .whitespacesAndNewlines)
        shell("launchctl kickstart -k gui/\(uid)/com.openclaw.gateway 2>/dev/null || openclaw restart 2>/dev/null")
    }

    func restartMemoryServer() {
        shell("pkill -f memory_server.py; sleep 1; nohup python3 ~/.openclaw/memory_server.py > /tmp/memory_server.log 2>&1 &")
    }

    func startOllama() {
        shell("nohup ollama serve > /tmp/ollama.log 2>&1 &")
    }

    func stopOllama() {
        shell("pkill -x ollama")
    }

    // MARK: - Helpers

    private func appURL(_ name: String) -> URL? {
        let candidates = [
            "/Applications/\(name).app",
            "\(NSHomeDirectory())/Applications/\(name).app",
        ]
        return candidates.compactMap { URL(fileURLWithPath: $0) }
                         .first { FileManager.default.fileExists(atPath: $0.path) }
    }

    private func bundleId(for name: String) -> String {
        let map: [String: String] = [
            "NovaControl":  "net.digitalnoise.NovaControl",
            "NMAPScanner":  "net.digitalnoise.nmapscanner.macos",
            "MLX Code":     "net.digitalnoise.mlxcode",
            "OneOnOne":     "net.digitalnoise.OneOnOne",
            "RsyncGUI":     "net.digitalnoise.RsyncGUI",
            "JiraSummary":  "net.digitalnoise.JiraSummary",
            "Mail Summary": "net.digitalnoise.MailSummary",
            "Nova-Desktop": "net.digitalnoise.Nova-Desktop",
        ]
        return map[name] ?? "net.digitalnoise.\(name.replacingOccurrences(of: " ", with: ""))"
    }
}
