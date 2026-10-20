//
//  MenuBarHealth.swift
//  Nova Cluster (Mac)
//
//  The menu-bar item (1.1.0): fleet health at a glance — machines live, scheduler alerts, the UPS — plus
//  "Just published" and "Open Nova Cluster". Pure AppKit (NSStatusItem + NSMenu), deliberately outside
//  SwiftUI's scene graph: a SwiftUI MenuBarExtra made SwiftUI rebuild the main menu on every update and the
//  app beach-balled (1.1.0 dev, 2026-10-19). It redraws only when MenuBarStatus says the summary changed.
//  With the item showing, closing the window doesn't quit the app.
//

import AppKit

@MainActor
final class StatusItemController: NSObject {
    static let shared = StatusItemController()
    private var item: NSStatusItem?
    private weak var status: MenuBarStatus?
    private var openMain: (() -> Void)?
    private var defaultsObserver: NSObjectProtocol?

    static var enabled: Bool { UserDefaults.standard.object(forKey: MacAppDelegate.menuBarKey) as? Bool ?? true }

    /// Called once from the main window; later calls just refresh the "open" action.
    func attach(_ status: MenuBarStatus, openMain: @escaping () -> Void) {
        self.openMain = openMain
        guard self.status !== status else { return }
        self.status = status
        status.onChange = { [weak self] in self?.render() }
        defaultsObserver = NotificationCenter.default.addObserver(forName: UserDefaults.didChangeNotification,
                                                                  object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.syncVisibility() }
        }
        syncVisibility()
    }

    private func syncVisibility() {
        if Self.enabled, item == nil {
            item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
            render()
        } else if !Self.enabled, let i = item {
            NSStatusBar.system.removeStatusItem(i); item = nil
        }
    }

    private func render() {
        guard let item, let status else { return }
        let h = status.health
        item.button?.image = NSImage(systemSymbolName: h.level == .good ? "circle.hexagongrid.fill" : "exclamationmark.triangle.fill",
                                     accessibilityDescription: "Nova Cluster")
        item.button?.imagePosition = .imageLeading
        item.button?.title = " " + h.title
        item.button?.toolTip = h.lines.map(\.0).joined(separator: "\n")

        let menu = NSMenu()
        let head = NSMenuItem(title: "NOVA · CLUSTER", action: nil, keyEquivalent: "")
        head.isEnabled = false
        menu.addItem(head)
        for (text, level) in h.lines { menu.addItem(info(Self.dot(level) + "  " + text)) }
        for p in status.problems { menu.addItem(info("     · " + p)) }
        if let post = status.latest {
            menu.addItem(.separator())
            let it = NSMenuItem(title: "Just published: " + post.text, action: #selector(openPost(_:)), keyEquivalent: "")
            it.target = self; it.representedObject = post.url
            menu.addItem(it)
        }
        menu.addItem(.separator())
        let open = NSMenuItem(title: "Open Nova Cluster", action: #selector(openWindow), keyEquivalent: "o")
        open.target = self
        menu.addItem(open)
        let quit = NSMenuItem(title: "Quit Nova Cluster", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(quit)
        item.menu = menu
    }

    private func info(_ s: String) -> NSMenuItem {
        let it = NSMenuItem(title: s, action: nil, keyEquivalent: "")
        it.isEnabled = false
        return it
    }

    static func dot(_ l: NodeFacts.Level) -> String {
        switch l {
        case .good: return "🟢"
        case .warn: return "🟠"
        case .bad: return "🔴"
        case .quiet: return "⚪️"
        }
    }

    @objc private func openPost(_ sender: NSMenuItem) {
        if let url = sender.representedObject as? URL { NSWorkspace.shared.open(url) }
    }

    @objc private func openWindow() {
        NSApp.activate(ignoringOtherApps: true)
        openMain?()
    }
}

/// Whether any of the app's main windows can be seen (not hidden, minimized or fully covered).
enum MacVisibility {
    static let changes = NotificationCenter.default.publisher(for: NSWindow.didChangeOcclusionStateNotification)
        .merge(with: NotificationCenter.default.publisher(for: NSApplication.didHideNotification),
               NotificationCenter.default.publisher(for: NSApplication.didUnhideNotification),
               NotificationCenter.default.publisher(for: NSWindow.didMiniaturizeNotification),
               NotificationCenter.default.publisher(for: NSWindow.didDeminiaturizeNotification))

    @MainActor
    static func anyMainWindowVisible() -> Bool {
        guard !NSApp.isHidden else { return false }
        return NSApp.windows.contains { $0.canBecomeMain && $0.isVisible && !$0.isMiniaturized && $0.occlusionState.contains(.visible) }
    }
}
