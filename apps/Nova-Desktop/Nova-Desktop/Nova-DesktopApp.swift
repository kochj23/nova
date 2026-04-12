// Nova-DesktopApp.swift — Nova Desktop
// Written by Jordan Koch.

import SwiftUI

@main
struct NovaDesktopApp: App {
    @StateObject private var monitor = NovaMonitor.shared

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(monitor)
                .onAppear { monitor.startMonitoring(); NovaAPIServer.shared.start() }
                .onDisappear { monitor.stopMonitoring(); NovaAPIServer.shared.stop() }
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1500, height: 900)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("Nova") {
                Button("Refresh All") {
                    Task { await monitor.refresh() }
                }.keyboardShortcut("r", modifiers: .command)

                Button("Refresh GitHub") {
                    Task { await monitor.refreshGitHub() }
                }.keyboardShortcut("g", modifiers: [.command, .shift])

                Divider()

                Button("Restart OpenClaw Gateway") {
                    ServiceController.shared.restartOpenClawGateway()
                }.keyboardShortcut("o", modifiers: [.command, .shift])

                Button("Restart Memory Server") {
                    ServiceController.shared.restartMemoryServer()
                }.keyboardShortcut("m", modifiers: [.command, .shift])

                Button("Start Ollama") {
                    ServiceController.shared.startOllama()
                }.keyboardShortcut("l", modifiers: [.command, .shift])
            }
        }
    }
}
