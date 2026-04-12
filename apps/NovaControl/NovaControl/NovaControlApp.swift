// NovaControl — Unified API gateway replacing OneOnOne, NMAPScanner, RsyncGUI, TopGUI, News Summary
// Written by Jordan Koch
// Port 37400 · macOS 14.0+ · Menu bar only

import SwiftUI

@main
struct NovaControlApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    var body: some Scene {
        Settings { EmptyView() }
    }
}

@MainActor
class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem?
    var statusWindow: NSWindow?
    let apiServer = NovaAPIServer.shared
    let dataManager = DataManager.shared

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        setupMenuBar()
        apiServer.start()
        dataManager.startRefreshing()
    }

    func setupMenuBar() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem?.button {
            button.image = NSImage(systemSymbolName: "antenna.radiowaves.left.and.right",
                                   accessibilityDescription: "NovaControl")
            button.action = #selector(toggleWindow)
            button.target = self
        }
    }

    @objc func toggleWindow() {
        if let window = statusWindow, window.isVisible {
            window.orderOut(nil)
            return
        }
        showStatusWindow()
    }

    func showStatusWindow() {
        if statusWindow == nil {
            let contentView = StatusWindowView()
                .environmentObject(dataManager)
            let hostingView = NSHostingView(rootView: contentView)
            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 640, height: 500),
                styleMask: [.titled, .closable, .resizable, .miniaturizable],
                backing: .buffered,
                defer: false
            )
            window.title = "Nova Control"
            window.contentView = hostingView
            window.center()
            window.isReleasedWhenClosed = false
            window.level = .floating
            statusWindow = window
        }
        statusWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}
