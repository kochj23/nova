//
//  MinionConfig.swift
//  Nova Minion (iPad)
//
//  Hub URL + node name live in UserDefaults; the minion token lives in the
//  iOS Keychain. Nothing network-specific is baked into the binary — the hub
//  is supplied at setup (settings sheet, novaminion://setup deep link, or
//  NOVA_MINION_HUB / NOVA_MINION_NODE / NOVA_MINION_TOKEN on first launch).
//

import Foundation
import Security

struct MinionConfig: Equatable {
    var hub: String
    var node: String
    var token: String

    static let defaultNode = "ipad-air"
    private static let hubKey = "hub.url"
    private static let nodeKey = "hub.node"

    static var current: MinionConfig {
        let d = UserDefaults.standard
        return MinionConfig(hub: d.string(forKey: hubKey) ?? "",
                            node: d.string(forKey: nodeKey) ?? defaultNode,
                            token: Keychain.read() ?? "")
    }

    var isConfigured: Bool { fleetURL != nil }

    var fleetURL: URL? {
        var h = hub.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !h.isEmpty else { return nil }
        if !h.contains("://") { h = "http://" + h }
        while h.hasSuffix("/") { h.removeLast() }
        guard var comps = URLComponents(string: h + "/fleet"), comps.host != nil else { return nil }
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

    /// Apply `novaminion://setup?hub=…&node=…&token=…`. Missing fields keep their current value.
    @discardableResult
    static func apply(url: URL) -> Bool {
        guard url.scheme?.lowercased() == "novaminion",
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

    /// First-launch configuration from the environment (e.g. devicectl -e / DEVICECTL_CHILD_*).
    static func applyEnvironment() {
        let env = ProcessInfo.processInfo.environment
        guard env["NOVA_MINION_HUB"] != nil || env["NOVA_MINION_TOKEN"] != nil else { return }
        var cfg = current
        if let v = env["NOVA_MINION_HUB"] { cfg.hub = v }
        if let v = env["NOVA_MINION_NODE"] { cfg.node = v }
        if let v = env["NOVA_MINION_TOKEN"] { cfg.token = v }
        cfg.save()
    }
}

enum Keychain {
    private static let service = "net.digitalnoise.NovaMinion"
    private static let account = "minion-token"

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
        SecItemDelete(query as CFDictionary)
        guard !value.isEmpty else { return }
        var q = query
        q[kSecValueData as String] = Data(value.utf8)
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(q as CFDictionary, nil)
    }
}
