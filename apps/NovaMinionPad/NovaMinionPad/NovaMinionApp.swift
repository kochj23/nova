//
//  NovaMinionApp.swift
//  Nova Minion (iPad + Mac)
//
//  Display-only companion to the Nova Minion screensaver: shows what Nova's
//  minion fleet is reading and learning, polled from Nova's hub.
//  iOS: one full-screen window. macOS: a resizable window (full-screen capable),
//  a standard Settings scene (⌘,) and View › Refresh Now (⌘R).
//

import SwiftUI

@main
struct NovaMinionApp: App {
    @State private var fleet = FleetModel()
    #if os(macOS)
    @NSApplicationDelegateAdaptor(MacAppDelegate.self) private var appDelegate
    #endif

    init() {
        // First-launch setup from NOVA_MINION_HUB / _NODE / _TOKEN, if present.
        // `-demoMinions N` and `-simLandscape YES` arrive via the UserDefaults argument domain.
        MinionConfig.applyEnvironment()
    }

    var body: some Scene {
        #if os(iOS)
        WindowGroup {
            Group {
                if UserDefaults.standard.bool(forKey: "simLandscape") {
                    // Screenshot aid for simulators that cannot rotate: lay out at
                    // landscape size, rotated into the portrait window.
                    GeometryReader { g in
                        ContentView(fleet: fleet)
                            .frame(width: g.size.height, height: g.size.width)
                            .rotationEffect(.degrees(-90))
                            .position(x: g.size.width / 2, y: g.size.height / 2)
                    }
                    .ignoresSafeArea()
                } else {
                    ContentView(fleet: fleet)
                }
            }
            .onAppear { UIApplication.shared.isIdleTimerDisabled = true }
        }
        #else
        Window("Nova Minion", id: "fleet") {
            ContentView(fleet: fleet)
                .frame(minWidth: 900, minHeight: 600)
        }
        .defaultSize(width: 1440, height: 900)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(before: .toolbar) {
                Button("Refresh Now") { Task { await fleet.pollOnce() } }
                    .keyboardShortcut("r", modifiers: .command)
                Divider()
            }
        }

        Settings {
            MacSettingsView(fleet: fleet)
        }
        #endif
    }
}

#if os(macOS)
import AppKit

final class MacAppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.appearance = NSAppearance(named: .darkAqua)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
#endif
