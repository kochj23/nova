//
//  NovaClusterApp.swift
//  Nova Cluster (iPad + Mac)
//
//  Live view of what Nova's whole cluster is doing with its language models — and
//  what that work turns into (the minions' reading and lessons) — polled from Nova's
//  hub every 2 s. iOS: one full-screen window, idle timer off. macOS: a resizable
//  window (full-screen capable), Settings (⌘,) and View › Refresh Now (⌘R).
//

import SwiftUI

@main
struct NovaClusterApp: App {
    @State private var cluster = ClusterModel()
    @AppStorage(KioskPolicy.enabledKey) private var kioskOn = KioskPolicy.defaultEnabled
    #if os(macOS)
    @NSApplicationDelegateAdaptor(MacAppDelegate.self) private var appDelegate
    #endif

    init() {
        ClusterConfig.applyEnvironment()
    }

    var body: some Scene {
        #if os(iOS)
        WindowGroup {
            ContentView(cluster: cluster)
                .onAppear { UIApplication.shared.isIdleTimerDisabled = true }
        }
        #else
        Window("Nova Cluster", id: "cluster") {
            ContentView(cluster: cluster)
                .frame(minWidth: 1000, minHeight: 680)
        }
        .defaultSize(width: 1440, height: 900)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(before: .toolbar) {
                Button("Cluster Map") { UserDefaults.standard.set(ViewMode.map.rawValue, forKey: ViewMode.key) }
                    .keyboardShortcut("1", modifiers: .command)
                Button("Dashboard") { UserDefaults.standard.set(ViewMode.dashboard.rawValue, forKey: ViewMode.key) }
                    .keyboardShortcut("2", modifiers: .command)
                Button("Constellation") { UserDefaults.standard.set(ViewMode.constellation.rawValue, forKey: ViewMode.key) }
                    .keyboardShortcut("3", modifiers: .command)
                Toggle("Kiosk Mode", isOn: $kioskOn)
                    .keyboardShortcut("k", modifiers: [.command, .shift])
                Divider()
                Button("Refresh Now") { Task { await cluster.pollOnce() } }
                    .keyboardShortcut("r", modifiers: .command)
                Divider()
            }
        }

        Settings {
            MacSettingsView(cluster: cluster)
        }

        #endif
    }
}

#if os(macOS)
import AppKit

final class MacAppDelegate: NSObject, NSApplicationDelegate {
    static let menuBarKey = StatusItemController.menuBarKey

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.appearance = NSAppearance(named: .darkAqua)
    }
    /// Closing the window quits, unless the menu-bar item is showing (then the app lives on up there).
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        !(UserDefaults.standard.object(forKey: Self.menuBarKey) as? Bool ?? true)
    }
}
#endif
