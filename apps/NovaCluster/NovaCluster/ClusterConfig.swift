//
//  ClusterConfig.swift
//  Nova Cluster (iPad + Mac)
//
//  Hub URL + viewer node live in UserDefaults; the token lives in the Keychain
//  (iOS: this-device-only; macOS: the login keychain, service = the app's bundle id,
//  account "cluster-token"). Nothing network-specific is compiled in — the hub is
//  supplied at setup (Settings, novacluster://setup deep link, or NOVA_CLUSTER_HUB /
//  NOVA_CLUSTER_NODE / NOVA_CLUSTER_TOKEN in the environment on launch).
//

import Foundation
import Security

struct ClusterConfig: Equatable {
    var hub: String
    var node: String
    var token: String

    #if os(macOS)
    static let defaultNode = "mac-studio-view"
    #else
    static let defaultNode = "ipad-air"
    #endif
    private static let hubKey = "hub.url"
    private static let nodeKey = "hub.node"

    static var current: ClusterConfig {
        let d = UserDefaults.standard
        return ClusterConfig(hub: d.string(forKey: hubKey) ?? "",
                             node: d.string(forKey: nodeKey) ?? defaultNode,
                             token: Keychain.read() ?? "")
    }

    var isConfigured: Bool { clusterURL != nil }

    var clusterURL: URL? {
        var h = hub.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !h.isEmpty else { return nil }
        if !h.contains("://") { h = "http://" + h }
        while h.hasSuffix("/") { h.removeLast() }
        guard var comps = URLComponents(string: h + "/cluster"), comps.host != nil else { return nil }
        let n = node.trimmingCharacters(in: .whitespacesAndNewlines)
        comps.queryItems = [URLQueryItem(name: "node", value: n.isEmpty ? Self.defaultNode : n)]
        return comps.url
    }

    func save() {
        let d = UserDefaults.standard
        d.set(hub.trimmingCharacters(in: .whitespacesAndNewlines), forKey: Self.hubKey)
        d.set(node.trimmingCharacters(in: .whitespacesAndNewlines), forKey: Self.nodeKey)
        Keychain.write(token.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    /// `novacluster://view?mode=map|dashboard|constellation` switches the view. Returns true if handled.
    static func applyView(url: URL) -> Bool {
        guard url.scheme?.lowercased() == "novacluster",
              let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
              (comps.host ?? "").lowercased() == "view",
              let m = comps.queryItems?.first(where: { $0.name == "mode" })?.value,
              let mode = ViewMode(rawValue: m) else { return false }
        UserDefaults.standard.set(mode.rawValue, forKey: ViewMode.key)
        return true
    }

    /// Apply `novacluster://setup?hub=…&node=…&token=…`. Missing fields keep their current value.
    @discardableResult
    static func apply(url: URL) -> Bool {
        guard url.scheme?.lowercased() == "novacluster",
              let comps = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return false }
        let host = (comps.host ?? comps.path).lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard host == "setup" else { return false }
        var cfg = current
        for q in comps.queryItems ?? [] {
            guard let v = q.value else { continue }
            switch q.name {
            case "hub": cfg.hub = v
            case "node": cfg.node = v
            case "token": cfg.token = v
            default: break
            }
        }
        cfg.save()
        return true
    }

    /// Launch-time configuration from the environment (e.g. devicectl DEVICECTL_CHILD_*).
    static func applyEnvironment() {
        let env = ProcessInfo.processInfo.environment
        guard env["NOVA_CLUSTER_HUB"] != nil || env["NOVA_CLUSTER_TOKEN"] != nil else { return }
        var cfg = current
        if let v = env["NOVA_CLUSTER_HUB"] { cfg.hub = v }
        if let v = env["NOVA_CLUSTER_NODE"] { cfg.node = v }
        if let v = env["NOVA_CLUSTER_TOKEN"] { cfg.token = v }
        cfg.save()
    }
}

/// The three views (1.1.0: Nova Minion's constellation joined the map and the dashboard).
/// Tap the wordmark to step through them; Mac: ⌘1 / ⌘2 / ⌘3; kiosk mode cycles them.
enum ViewMode: String, CaseIterable {
    case map, dashboard, constellation
    static let key = "view.mode"

    /// Unknown or missing stored values fall back to the map.
    init(stored: String?) { self = ViewMode(rawValue: stored ?? "") ?? .map }

    var next: ViewMode {
        let all = Self.allCases
        return all[(all.firstIndex(of: self)! + 1) % all.count]
    }

    /// Wordmark subtitle.
    var subtitle: String {
        switch self {
        case .map: return "· CLUSTER"
        case .dashboard: return "· DASHBOARD"
        case .constellation: return "· MINION"
        }
    }
}

/// Kiosk mode (1.1.0): for the iPad on its stand, a Mac in full screen, or the Apple TV.
/// Steps map → dashboard → constellation every `interval` seconds; any touch, click, key
/// or pointer movement pauses the cycle (for `pause` seconds) so someone can look at a view.
struct KioskPolicy: Equatable {
    var enabled: Bool
    var interval: TimeInterval

    static let enabledKey = "kiosk.enabled"
    static let intervalKey = "kiosk.interval"
    static let defaultInterval: TimeInterval = 60
    static let intervalRange: ClosedRange<TimeInterval> = 10...3600
    #if os(tvOS)
    static let defaultEnabled = true          // the TV is a wall display
    #else
    static let defaultEnabled = false
    #endif

    /// Paused after a touch for at least two minutes, longer if the interval is longer.
    var pause: TimeInterval { max(120, interval) }

    static func clamp(_ v: TimeInterval) -> TimeInterval {
        v.isFinite && v > 0 ? min(intervalRange.upperBound, max(intervalRange.lowerBound, v)) : defaultInterval
    }

    static func load(_ d: UserDefaults = .standard) -> KioskPolicy {
        let on = d.object(forKey: enabledKey) as? Bool ?? defaultEnabled
        let iv = d.object(forKey: intervalKey) as? Double ?? defaultInterval
        return KioskPolicy(enabled: on, interval: clamp(iv))
    }

    /// True when the cycle should move to the next view now.
    func shouldAdvance(now: Date, lastSwitch: Date, lastTouch: Date?) -> Bool {
        guard enabled else { return false }
        if let t = lastTouch, now.timeIntervalSince(t) < pause { return false }
        return now.timeIntervalSince(max(lastSwitch, lastTouch ?? .distantPast)) >= interval
    }

    /// `novacluster://kiosk?on=1&interval=60` (either field optional). Returns true if handled.
    @discardableResult
    static func apply(url: URL, defaults d: UserDefaults = .standard) -> Bool {
        guard url.scheme?.lowercased() == "novacluster",
              let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
              (comps.host ?? "").lowercased() == "kiosk" else { return false }
        for q in comps.queryItems ?? [] {
            guard let v = q.value else { continue }
            switch q.name {
            case "on": d.set(["1", "true", "yes", "on"].contains(v.lowercased()), forKey: enabledKey)
            case "interval": if let x = Double(v) { d.set(clamp(x), forKey: intervalKey) }
            default: break
            }
        }
        return true
    }
}

enum Keychain {
    /// iOS: net.digitalnoise.NovaClusterPad · macOS: net.digitalnoise.NovaCluster
    private static let service = Bundle.main.bundleIdentifier ?? "net.digitalnoise.NovaCluster"
    private static let account = "cluster-token"

    private static var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    static func read() -> String? {
        var q = query
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func write(_ value: String) {
        if value == read() { return }
        SecItemDelete(query as CFDictionary)
        guard !value.isEmpty else { return }
        var q = query
        q[kSecValueData as String] = Data(value.utf8)
        #if os(iOS)
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        #endif
        SecItemAdd(q as CFDictionary, nil)
    }
}
