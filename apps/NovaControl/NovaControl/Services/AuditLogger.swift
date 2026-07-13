// NovaControl — Audit Trail Logger
// Written by Jordan Koch
// Records all POST mutations to a local SQLite database for compliance and debugging.

import Foundation
import SQLite3

/// Thread-safe audit logger backed by SQLite. Records every mutation (POST) with
/// timestamp, endpoint, requester context, request body, and response status.
final class AuditLogger {
    static let shared = AuditLogger()

    private var db: OpaquePointer?
    private let queue = DispatchQueue(label: "net.digitalnoise.novacontrol.audit", qos: .utility)

    private init() {
        openDatabase()
        createTable()
    }

    deinit {
        if let db = db { sqlite3_close(db) }
    }

    // MARK: - Database Setup

    private func openDatabase() {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let dir = support.appendingPathComponent("NovaControl", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let dbPath = dir.appendingPathComponent("audit.sqlite3").path

        if sqlite3_open(dbPath, &db) != SQLITE_OK {
            NSLog("[AuditLogger] Failed to open database at \(dbPath)")
            db = nil
        }

        // Enable WAL mode for better concurrent read performance
        sqlite3_exec(db, "PRAGMA journal_mode=WAL;", nil, nil, nil)
    }

    private func createTable() {
        let sql = """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            request_body TEXT,
            response_status INTEGER NOT NULL,
            response_summary TEXT,
            source_ip TEXT DEFAULT '127.0.0.1',
            duration_ms INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
        CREATE INDEX IF NOT EXISTS idx_audit_path ON audit_log(path);
        """
        sqlite3_exec(db, sql, nil, nil, nil)
    }

    // MARK: - Recording

    /// Record a mutation event. Call this after processing any POST request.
    func record(method: String, path: String, requestBody: String?,
                responseStatus: Int, responseSummary: String?, durationMs: Int = 0) {
        queue.async { [weak self] in
            guard let self = self, let db = self.db else { return }

            let sql = """
            INSERT INTO audit_log (method, path, request_body, response_status, response_summary, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?);
            """
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return }
            defer { sqlite3_finalize(stmt) }

            sqlite3_bind_text(stmt, 1, (method as NSString).utf8String, -1, nil)
            sqlite3_bind_text(stmt, 2, (path as NSString).utf8String, -1, nil)
            if let body = requestBody {
                // Truncate large bodies in audit log to keep DB lean
                let truncated = body.count > 4096 ? String(body.prefix(4096)) + "...[truncated]" : body
                sqlite3_bind_text(stmt, 3, (truncated as NSString).utf8String, -1, nil)
            } else {
                sqlite3_bind_null(stmt, 3)
            }
            sqlite3_bind_int(stmt, 4, Int32(responseStatus))
            if let summary = responseSummary {
                sqlite3_bind_text(stmt, 5, (summary as NSString).utf8String, -1, nil)
            } else {
                sqlite3_bind_null(stmt, 5)
            }
            sqlite3_bind_int(stmt, 6, Int32(durationMs))

            if sqlite3_step(stmt) != SQLITE_DONE {
                NSLog("[AuditLogger] Insert failed: \(String(cString: sqlite3_errmsg(db)))")
            }
        }
    }

    // MARK: - Querying

    struct AuditEntry {
        let id: Int
        let timestamp: String
        let method: String
        let path: String
        let requestBody: String?
        let responseStatus: Int
        let responseSummary: String?
        let durationMs: Int
    }

    /// Query audit log with optional filters.
    func query(limit: Int = 50, path: String? = nil, since: String? = nil,
               status: Int? = nil) -> [AuditEntry] {
        var entries: [AuditEntry] = []
        queue.sync { [weak self] in
            guard let self = self, let db = self.db else { return }

            var conditions: [String] = []
            var bindings: [Any] = []

            if let path = path {
                conditions.append("path LIKE ?")
                bindings.append("%\(path)%")
            }
            if let since = since {
                conditions.append("timestamp >= ?")
                bindings.append(since)
            }
            if let status = status {
                conditions.append("response_status = ?")
                bindings.append(status)
            }

            var sql = "SELECT id, timestamp, method, path, request_body, response_status, response_summary, duration_ms FROM audit_log"
            if !conditions.isEmpty {
                sql += " WHERE " + conditions.joined(separator: " AND ")
            }
            sql += " ORDER BY id DESC LIMIT \(min(limit, 500))"

            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return }
            defer { sqlite3_finalize(stmt) }

            // Bind parameters
            for (i, binding) in bindings.enumerated() {
                let idx = Int32(i + 1)
                if let strVal = binding as? String {
                    sqlite3_bind_text(stmt, idx, (strVal as NSString).utf8String, -1, nil)
                } else if let intVal = binding as? Int {
                    sqlite3_bind_int(stmt, idx, Int32(intVal))
                }
            }

            while sqlite3_step(stmt) == SQLITE_ROW {
                let entry = AuditEntry(
                    id: Int(sqlite3_column_int(stmt, 0)),
                    timestamp: String(cString: sqlite3_column_text(stmt, 1)),
                    method: String(cString: sqlite3_column_text(stmt, 2)),
                    path: String(cString: sqlite3_column_text(stmt, 3)),
                    requestBody: sqlite3_column_text(stmt, 4).map { String(cString: $0) },
                    responseStatus: Int(sqlite3_column_int(stmt, 5)),
                    responseSummary: sqlite3_column_text(stmt, 6).map { String(cString: $0) },
                    durationMs: Int(sqlite3_column_int(stmt, 7))
                )
                entries.append(entry)
            }
        }
        return entries
    }

    /// Returns total count of audit entries (for stats).
    func totalCount() -> Int {
        var count = 0
        queue.sync { [weak self] in
            guard let self = self, let db = self.db else { return }
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM audit_log", -1, &stmt, nil) == SQLITE_OK else { return }
            defer { sqlite3_finalize(stmt) }
            if sqlite3_step(stmt) == SQLITE_ROW {
                count = Int(sqlite3_column_int(stmt, 0))
            }
        }
        return count
    }
}
