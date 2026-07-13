// NovaControl — API Key Management
// Written by Jordan Koch
// Generates and validates API keys stored in macOS Keychain with per-key scopes.
// Provides middleware-style authentication for all request types.

import Foundation
import CryptoKit
import Security

// MARK: - API Key Model

struct APIKey: Codable, Identifiable {
    let id: String               // SHA-256 hash prefix (first 8 chars) for identification
    let name: String             // Human-readable label (e.g. "Nova Gateway", "Dashboard")
    let createdAt: Date
    let scopes: [APIScope]       // Permitted operations
    let expiresAt: Date?         // Optional expiration
    var lastUsed: Date?
    var useCount: Int

    enum APIScope: String, Codable, CaseIterable {
        case read           // GET requests
        case write          // POST requests (mutations)
        case admin          // Admin operations (key management, audit)
        case scan           // NMAP scan operations
        case rsync          // Rsync job execution
        case workflows      // Workflow execution
        case alerts         // Alert management
    }
}

// MARK: - API Key Manager

final class APIKeyManager {
    static let shared = APIKeyManager()

    private let keychainPrefix = "net.digitalnoise.novacontrol.apikey."
    private let metadataURL: URL = {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let dir = support.appendingPathComponent("NovaControl/Auth", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("api-keys-metadata.json")
    }()

    private var keyMetadata: [APIKey] = []
    private let queue = DispatchQueue(label: "net.digitalnoise.novacontrol.apikeys")

    private init() {
        loadMetadata()
        ensureDefaultKey()
    }

    // MARK: - Key Generation

    /// Generates a new API key with specified scopes. Returns the raw key string (only shown once).
    func generateKey(name: String, scopes: [APIKey.APIScope], expiresAt: Date? = nil) -> (key: String, metadata: APIKey) {
        // Generate a cryptographically secure random key
        var keyBytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, keyBytes.count, &keyBytes)
        let rawKey = "nckey_" + Data(keyBytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")

        // Create hash-based ID for identification without exposing the key
        let keyHash = SHA256.hash(data: Data(rawKey.utf8))
        let hashPrefix = keyHash.compactMap { String(format: "%02x", $0) }.joined().prefix(8)
        let keyId = String(hashPrefix)

        let metadata = APIKey(
            id: keyId, name: name, createdAt: Date(),
            scopes: scopes, expiresAt: expiresAt,
            lastUsed: nil, useCount: 0
        )

        // Store the raw key in Keychain
        KeychainHelper.save(key: keychainPrefix + keyId, value: rawKey)

        // Store metadata
        queue.sync {
            keyMetadata.append(metadata)
            saveMetadata()
        }

        NSLog("[APIKeyManager] Generated key '\(name)' (id: \(keyId)) with scopes: \(scopes.map { $0.rawValue })")
        return (rawKey, metadata)
    }

    /// Validates a raw API key and returns the associated metadata if valid.
    func validate(rawKey: String) -> APIKey? {
        guard rawKey.hasPrefix("nckey_") else { return nil }

        // Compute hash to find matching key
        let keyHash = SHA256.hash(data: Data(rawKey.utf8))
        let hashPrefix = String(keyHash.compactMap { String(format: "%02x", $0) }.joined().prefix(8))

        return queue.sync {
            guard var metadata = keyMetadata.first(where: { $0.id == hashPrefix }) else {
                return nil
            }

            // Check expiration
            if let expires = metadata.expiresAt, Date() > expires {
                NSLog("[APIKeyManager] Key '\(metadata.name)' has expired")
                return nil
            }

            // Verify against Keychain
            guard let storedKey = KeychainHelper.load(key: keychainPrefix + hashPrefix),
                  storedKey == rawKey else {
                return nil
            }

            // Update usage stats
            metadata.lastUsed = Date()
            metadata.useCount += 1
            if let idx = keyMetadata.firstIndex(where: { $0.id == hashPrefix }) {
                keyMetadata[idx] = metadata
                saveMetadata()
            }

            return metadata
        }
    }

    /// Checks if a key has the required scope for an operation.
    func hasScope(_ key: APIKey, scope: APIKey.APIScope) -> Bool {
        return key.scopes.contains(scope) || key.scopes.contains(.admin)
    }

    /// Determines the required scope for a given HTTP method and path.
    func requiredScope(method: String, path: String) -> APIKey.APIScope {
        if path.hasPrefix("/api/keys") || path.hasPrefix("/api/audit") {
            return .admin
        }
        if path.hasPrefix("/api/nmap/scan") {
            return .scan
        }
        if path.hasPrefix("/api/rsync/") && path.hasSuffix("/run") {
            return .rsync
        }
        if path.hasPrefix("/api/workflows/") && path.hasSuffix("/run") {
            return .workflows
        }
        if path.hasPrefix("/api/alerts") && method == "POST" {
            return .alerts
        }
        if method == "POST" {
            return .write
        }
        return .read
    }

    // MARK: - Key Management

    /// Revoke (delete) a key by its ID.
    func revokeKey(id: String) -> Bool {
        return queue.sync {
            KeychainHelper.delete(key: keychainPrefix + id)
            keyMetadata.removeAll { $0.id == id }
            saveMetadata()
            NSLog("[APIKeyManager] Revoked key id: \(id)")
            return true
        }
    }

    /// List all keys (metadata only, never the raw key).
    func listKeys() -> [APIKey] {
        return queue.sync { keyMetadata }
    }

    // MARK: - Authentication Middleware

    /// Authenticate a request. Returns the validated key or nil.
    /// Supports both Bearer token and X-API-Key header.
    func authenticate(authorization: String?, apiKeyHeader: String?) -> APIKey? {
        // Try Bearer token first
        if let auth = authorization {
            let token: String
            if auth.hasPrefix("Bearer ") {
                token = String(auth.dropFirst(7))
            } else {
                token = auth
            }
            if let key = validate(rawKey: token) {
                return key
            }
        }

        // Try X-API-Key header
        if let keyHeader = apiKeyHeader, let key = validate(rawKey: keyHeader) {
            return key
        }

        return nil
    }

    // MARK: - Default Key

    private func ensureDefaultKey() {
        if keyMetadata.isEmpty {
            // Also check legacy token and migrate
            let legacyKey = "net.digitalnoise.novacontrol.apitoken"
            if let legacyToken = KeychainHelper.load(key: legacyKey) {
                // Keep the legacy token working as a full-access key
                let keyHash = SHA256.hash(data: Data(legacyToken.utf8))
                let hashPrefix = String(keyHash.compactMap { String(format: "%02x", $0) }.joined().prefix(8))

                let metadata = APIKey(
                    id: hashPrefix, name: "Legacy Token (migrated)",
                    createdAt: Date(), scopes: APIKey.APIScope.allCases,
                    expiresAt: nil, lastUsed: nil, useCount: 0
                )
                KeychainHelper.save(key: keychainPrefix + hashPrefix, value: legacyToken)
                keyMetadata.append(metadata)
                saveMetadata()
                NSLog("[APIKeyManager] Migrated legacy token as full-access key")
            } else {
                // Generate a default admin key
                let (key, _) = generateKey(name: "Default Admin", scopes: APIKey.APIScope.allCases)
                NSLog("[APIKeyManager] Generated default admin key. Store this securely: \(key)")
            }
        }
    }

    // MARK: - Persistence

    private func loadMetadata() {
        guard let data = try? Data(contentsOf: metadataURL),
              let loaded = try? JSONDecoder().decode([APIKey].self, from: data) else { return }
        keyMetadata = loaded
    }

    private func saveMetadata() {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        guard let data = try? encoder.encode(keyMetadata) else { return }
        try? data.write(to: metadataURL)
    }
}
