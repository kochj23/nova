// NovaControl — Scheduled Health Reports
// Written by Jordan Koch
// Buffers metrics in SQLite ring buffer. Generates hourly/daily reports
// with trend analysis (z-score anomaly detection). Auto-posts to Slack.

import Foundation
import SQLite3

// MARK: - Metric Sample

struct MetricSample {
    let name: String
    let value: Double
    let timestamp: Date
}

// MARK: - Health Report

struct HealthReport: Codable {
    let id: UUID
    let generatedAt: String
    let periodStart: String
    let periodEnd: String
    let periodType: String       // "hourly" or "daily"
    let metrics: [MetricSummary]
    let anomalies: [Anomaly]
    let overallHealth: String    // "healthy", "degraded", "critical"

    struct MetricSummary: Codable {
        let name: String
        let min: Double
        let max: Double
        let avg: Double
        let latest: Double
        let sampleCount: Int
        let trend: String        // "stable", "rising", "falling"
    }

    struct Anomaly: Codable {
        let metricName: String
        let value: Double
        let zScore: Double
        let timestamp: String
        let severity: String     // "warning" (z>2), "critical" (z>3)
    }
}

// MARK: - Health Reporter

final class HealthReporter {
    static let shared = HealthReporter()

    private var db: OpaquePointer?
    private let queue = DispatchQueue(label: "net.digitalnoise.novacontrol.healthreporter", qos: .utility)
    private var hourlyTimer: DispatchSourceTimer?
    private var dailyTimer: DispatchSourceTimer?

    /// Ring buffer max: 7 days of samples at 1-minute intervals = ~10,080 rows
    private let maxSamples = 10_080
    private var latestReport: HealthReport?

    private init() {
        openDatabase()
        createTables()
        startTimers()
    }

    deinit {
        hourlyTimer?.cancel()
        dailyTimer?.cancel()
        if let db = db { sqlite3_close(db) }
    }

    // MARK: - Database Setup

    private func openDatabase() {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let dir = support.appendingPathComponent("NovaControl", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let dbPath = dir.appendingPathComponent("metrics.sqlite3").path

        if sqlite3_open(dbPath, &db) != SQLITE_OK {
            NSLog("[HealthReporter] Failed to open metrics database")
            db = nil
        }
        sqlite3_exec(db, "PRAGMA journal_mode=WAL;", nil, nil, nil)
    }

    private func createTables() {
        let sql = """
        CREATE TABLE IF NOT EXISTS metric_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            value REAL NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE INDEX IF NOT EXISTS idx_metrics_name_ts ON metric_samples(name, timestamp);
        CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metric_samples(timestamp);

        CREATE TABLE IF NOT EXISTS health_reports (
            id TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            period_type TEXT NOT NULL,
            report_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reports_generated ON health_reports(generated_at);
        """
        sqlite3_exec(db, sql, nil, nil, nil)
    }

    // MARK: - Recording Metrics

    /// Record a batch of metric samples (called on each refresh cycle).
    func record(stats: SystemStats?, novaOnline: Bool, memoriesCount: Int,
                deviceCount: Int, threatCount: Int, cronErrors: Int) {
        queue.async { [weak self] in
            guard let self = self, let db = self.db else { return }

            var samples: [(String, Double)] = []
            if let stats = stats {
                samples.append(("cpu_percent", stats.cpuUser + stats.cpuSystem))
                samples.append(("cpu_user", stats.cpuUser))
                samples.append(("cpu_system", stats.cpuSystem))
                samples.append(("mem_used_gb", stats.memUsedGB))
                samples.append(("mem_percent", stats.memTotalGB > 0 ? (stats.memUsedGB / stats.memTotalGB) * 100.0 : 0))
                samples.append(("disk_read_mbs", stats.diskReadMBs))
                samples.append(("disk_write_mbs", stats.diskWriteMBs))
            }
            samples.append(("nova_online", novaOnline ? 1.0 : 0.0))
            samples.append(("memories_count", Double(memoriesCount)))
            samples.append(("device_count", Double(deviceCount)))
            samples.append(("threat_count", Double(threatCount)))
            samples.append(("cron_errors", Double(cronErrors)))

            let sql = "INSERT INTO metric_samples (name, value) VALUES (?, ?)"
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return }
            defer { sqlite3_finalize(stmt) }

            for (name, value) in samples {
                sqlite3_bind_text(stmt, 1, (name as NSString).utf8String, -1, nil)
                sqlite3_bind_double(stmt, 2, value)
                sqlite3_step(stmt)
                sqlite3_reset(stmt)
            }

            // Ring buffer cleanup
            self.pruneOldSamples()
        }
    }

    private func pruneOldSamples() {
        guard let db = db else { return }
        let sql = "DELETE FROM metric_samples WHERE id NOT IN (SELECT id FROM metric_samples ORDER BY id DESC LIMIT \(maxSamples))"
        sqlite3_exec(db, sql, nil, nil, nil)
    }

    // MARK: - Report Generation

    /// Generate a health report for the given period.
    func generateReport(periodType: String = "hourly") -> HealthReport {
        let iso = ISO8601DateFormatter()
        let now = Date()
        let periodStart: Date
        switch periodType {
        case "daily":
            periodStart = now.addingTimeInterval(-86400)
        default:
            periodStart = now.addingTimeInterval(-3600)
        }

        let metrics = computeMetricSummaries(since: periodStart)
        let anomalies = detectAnomalies(since: periodStart)

        // Determine overall health
        let overallHealth: String
        let criticalAnomalies = anomalies.filter { $0.severity == "critical" }
        if !criticalAnomalies.isEmpty {
            overallHealth = "critical"
        } else if !anomalies.isEmpty {
            overallHealth = "degraded"
        } else {
            overallHealth = "healthy"
        }

        let report = HealthReport(
            id: UUID(),
            generatedAt: iso.string(from: now),
            periodStart: iso.string(from: periodStart),
            periodEnd: iso.string(from: now),
            periodType: periodType,
            metrics: metrics,
            anomalies: anomalies,
            overallHealth: overallHealth
        )

        // Store report
        storeReport(report)
        latestReport = report

        return report
    }

    private func computeMetricSummaries(since: Date) -> [HealthReport.MetricSummary] {
        var summaries: [HealthReport.MetricSummary] = []
        let iso = ISO8601DateFormatter()
        let sinceStr = iso.string(from: since)

        let metricNames = ["cpu_percent", "mem_percent", "mem_used_gb",
                           "disk_read_mbs", "disk_write_mbs", "nova_online",
                           "memories_count", "device_count", "threat_count", "cron_errors"]

        queue.sync { [weak self] in
            guard let self = self, let db = self.db else { return }

            for name in metricNames {
                let sql = """
                SELECT MIN(value), MAX(value), AVG(value), COUNT(value)
                FROM metric_samples WHERE name = ? AND timestamp >= ?
                """
                var stmt: OpaquePointer?
                guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { continue }
                defer { sqlite3_finalize(stmt) }

                sqlite3_bind_text(stmt, 1, (name as NSString).utf8String, -1, nil)
                sqlite3_bind_text(stmt, 2, (sinceStr as NSString).utf8String, -1, nil)

                guard sqlite3_step(stmt) == SQLITE_ROW else { continue }
                let minVal = sqlite3_column_double(stmt, 0)
                let maxVal = sqlite3_column_double(stmt, 1)
                let avgVal = sqlite3_column_double(stmt, 2)
                let count = Int(sqlite3_column_int(stmt, 3))
                guard count > 0 else { continue }

                // Get latest value
                let latestSQL = "SELECT value FROM metric_samples WHERE name = ? ORDER BY id DESC LIMIT 1"
                var latestStmt: OpaquePointer?
                var latest = avgVal
                if sqlite3_prepare_v2(db, latestSQL, -1, &latestStmt, nil) == SQLITE_OK {
                    sqlite3_bind_text(latestStmt, 1, (name as NSString).utf8String, -1, nil)
                    if sqlite3_step(latestStmt) == SQLITE_ROW {
                        latest = sqlite3_column_double(latestStmt, 0)
                    }
                    sqlite3_finalize(latestStmt)
                }

                // Compute trend: compare first half avg to second half avg
                let trend: String
                if count < 4 {
                    trend = "stable"
                } else {
                    let halfSQL = """
                    SELECT AVG(CASE WHEN rownum <= ? THEN value END),
                           AVG(CASE WHEN rownum > ? THEN value END)
                    FROM (SELECT value, ROW_NUMBER() OVER (ORDER BY id) as rownum
                          FROM metric_samples WHERE name = ? AND timestamp >= ?)
                    """
                    var halfStmt: OpaquePointer?
                    var firstHalf = avgVal
                    var secondHalf = avgVal
                    if sqlite3_prepare_v2(db, halfSQL, -1, &halfStmt, nil) == SQLITE_OK {
                        let half = Int32(count / 2)
                        sqlite3_bind_int(halfStmt, 1, half)
                        sqlite3_bind_int(halfStmt, 2, half)
                        sqlite3_bind_text(halfStmt, 3, (name as NSString).utf8String, -1, nil)
                        sqlite3_bind_text(halfStmt, 4, (sinceStr as NSString).utf8String, -1, nil)
                        if sqlite3_step(halfStmt) == SQLITE_ROW {
                            firstHalf = sqlite3_column_double(halfStmt, 0)
                            secondHalf = sqlite3_column_double(halfStmt, 1)
                        }
                        sqlite3_finalize(halfStmt)
                    }
                    let delta = secondHalf - firstHalf
                    let threshold = avgVal * 0.1 // 10% change = significant
                    if delta > threshold {
                        trend = "rising"
                    } else if delta < -threshold {
                        trend = "falling"
                    } else {
                        trend = "stable"
                    }
                }

                summaries.append(HealthReport.MetricSummary(
                    name: name, min: minVal, max: maxVal, avg: avgVal,
                    latest: latest, sampleCount: count, trend: trend
                ))
            }
        }

        return summaries
    }

    // MARK: - Anomaly Detection (Z-Score)

    private func detectAnomalies(since: Date) -> [HealthReport.Anomaly] {
        var anomalies: [HealthReport.Anomaly] = []
        let iso = ISO8601DateFormatter()
        let sinceStr = iso.string(from: since)

        let metricNames = ["cpu_percent", "mem_percent", "disk_write_mbs"]

        queue.sync { [weak self] in
            guard let self = self, let db = self.db else { return }

            for name in metricNames {
                // Compute mean and stddev from historical data (last 24h for baseline)
                let baselineSince = iso.string(from: Date().addingTimeInterval(-86400))
                let statsSQL = "SELECT AVG(value), COUNT(value) FROM metric_samples WHERE name = ? AND timestamp >= ?"
                var statsStmt: OpaquePointer?
                guard sqlite3_prepare_v2(db, statsSQL, -1, &statsStmt, nil) == SQLITE_OK else { continue }
                sqlite3_bind_text(statsStmt, 1, (name as NSString).utf8String, -1, nil)
                sqlite3_bind_text(statsStmt, 2, (baselineSince as NSString).utf8String, -1, nil)
                guard sqlite3_step(statsStmt) == SQLITE_ROW else {
                    sqlite3_finalize(statsStmt)
                    continue
                }
                let mean = sqlite3_column_double(statsStmt, 0)
                let totalCount = sqlite3_column_int(statsStmt, 1)
                sqlite3_finalize(statsStmt)

                guard totalCount > 10 else { continue } // Need enough data for meaningful stats

                // Compute stddev
                let stddevSQL = """
                SELECT SQRT(AVG((value - ?) * (value - ?)))
                FROM metric_samples WHERE name = ? AND timestamp >= ?
                """
                var stddevStmt: OpaquePointer?
                guard sqlite3_prepare_v2(db, stddevSQL, -1, &stddevStmt, nil) == SQLITE_OK else { continue }
                sqlite3_bind_double(stddevStmt, 1, mean)
                sqlite3_bind_double(stddevStmt, 2, mean)
                sqlite3_bind_text(stddevStmt, 3, (name as NSString).utf8String, -1, nil)
                sqlite3_bind_text(stddevStmt, 4, (baselineSince as NSString).utf8String, -1, nil)
                guard sqlite3_step(stddevStmt) == SQLITE_ROW else {
                    sqlite3_finalize(stddevStmt)
                    continue
                }
                let stddev = sqlite3_column_double(stddevStmt, 0)
                sqlite3_finalize(stddevStmt)

                guard stddev > 0.001 else { continue } // Avoid division by near-zero

                // Check recent samples for anomalies
                let recentSQL = "SELECT value, timestamp FROM metric_samples WHERE name = ? AND timestamp >= ? ORDER BY id DESC LIMIT 10"
                var recentStmt: OpaquePointer?
                guard sqlite3_prepare_v2(db, recentSQL, -1, &recentStmt, nil) == SQLITE_OK else { continue }
                sqlite3_bind_text(recentStmt, 1, (name as NSString).utf8String, -1, nil)
                sqlite3_bind_text(recentStmt, 2, (sinceStr as NSString).utf8String, -1, nil)

                while sqlite3_step(recentStmt) == SQLITE_ROW {
                    let value = sqlite3_column_double(recentStmt, 0)
                    let ts = String(cString: sqlite3_column_text(recentStmt, 1))
                    let zScore = (value - mean) / stddev

                    if abs(zScore) > 2.0 {
                        let severity = abs(zScore) > 3.0 ? "critical" : "warning"
                        anomalies.append(HealthReport.Anomaly(
                            metricName: name, value: value, zScore: zScore,
                            timestamp: ts, severity: severity
                        ))
                    }
                }
                sqlite3_finalize(recentStmt)
            }
        }

        return anomalies
    }

    // MARK: - Report Storage

    private func storeReport(_ report: HealthReport) {
        queue.async { [weak self] in
            guard let self = self, let db = self.db else { return }
            guard let json = try? JSONEncoder().encode(report),
                  let jsonStr = String(data: json, encoding: .utf8) else { return }

            let sql = "INSERT OR REPLACE INTO health_reports (id, generated_at, period_type, report_json) VALUES (?, ?, ?, ?)"
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return }
            defer { sqlite3_finalize(stmt) }

            sqlite3_bind_text(stmt, 1, (report.id.uuidString as NSString).utf8String, -1, nil)
            sqlite3_bind_text(stmt, 2, (report.generatedAt as NSString).utf8String, -1, nil)
            sqlite3_bind_text(stmt, 3, (report.periodType as NSString).utf8String, -1, nil)
            sqlite3_bind_text(stmt, 4, (jsonStr as NSString).utf8String, -1, nil)
            sqlite3_step(stmt)

            // Keep only last 168 hourly + 30 daily reports
            let pruneSQL = """
            DELETE FROM health_reports WHERE id NOT IN (
                SELECT id FROM health_reports WHERE period_type = 'hourly' ORDER BY generated_at DESC LIMIT 168
                UNION ALL
                SELECT id FROM health_reports WHERE period_type = 'daily' ORDER BY generated_at DESC LIMIT 30
            )
            """
            sqlite3_exec(db, pruneSQL, nil, nil, nil)
        }
    }

    // MARK: - Scheduled Timers

    private func startTimers() {
        // Hourly report timer
        let hourly = DispatchSource.makeTimerSource(queue: queue)
        hourly.schedule(deadline: .now() + 3600, repeating: 3600)
        hourly.setEventHandler { [weak self] in
            guard let self = self else { return }
            let report = self.generateReport(periodType: "hourly")
            NSLog("[HealthReporter] Hourly report: \(report.overallHealth) · \(report.anomalies.count) anomalies")
            if report.overallHealth != "healthy" {
                Task { await self.postToSlack(report: report) }
            }
        }
        hourly.resume()
        hourlyTimer = hourly

        // Daily report timer
        let daily = DispatchSource.makeTimerSource(queue: queue)
        daily.schedule(deadline: .now() + 86400, repeating: 86400)
        daily.setEventHandler { [weak self] in
            guard let self = self else { return }
            let report = self.generateReport(periodType: "daily")
            NSLog("[HealthReporter] Daily report: \(report.overallHealth) · \(report.metrics.count) metrics")
            Task { await self.postToSlack(report: report) }
        }
        daily.resume()
        dailyTimer = daily
    }

    // MARK: - Slack Integration

    private func postToSlack(report: HealthReport) async {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath:
                    NSHomeDirectory() + "/.openclaw/openclaw.json")),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let channels = json["channels"] as? [String: Any],
              let slack = channels["slack"] as? [String: Any],
              let token = slack["botToken"] as? String, !token.isEmpty else { return }

        let emoji: String
        switch report.overallHealth {
        case "critical": emoji = ":red_circle:"
        case "degraded": emoji = ":large_yellow_circle:"
        default:         emoji = ":large_green_circle:"
        }

        var lines = ["\(emoji) *NovaControl \(report.periodType.capitalized) Health Report*"]
        lines.append("Period: \(report.periodStart) - \(report.periodEnd)")
        lines.append("Status: *\(report.overallHealth.uppercased())*")

        if !report.anomalies.isEmpty {
            lines.append("\n:warning: *Anomalies Detected:*")
            for anomaly in report.anomalies.prefix(5) {
                lines.append("  - \(anomaly.metricName): \(String(format: "%.2f", anomaly.value)) (z=\(String(format: "%.1f", anomaly.zScore)), \(anomaly.severity))")
            }
        }

        // Key metrics summary
        let keyMetrics = report.metrics.filter { ["cpu_percent", "mem_percent", "disk_write_mbs"].contains($0.name) }
        if !keyMetrics.isEmpty {
            lines.append("\n*Key Metrics:*")
            for m in keyMetrics {
                lines.append("  - \(m.name): avg=\(String(format: "%.1f", m.avg)), max=\(String(format: "%.1f", m.max)), trend=\(m.trend)")
            }
        }

        let message = lines.joined(separator: "\n")
        let channel = "C0ATAF7NZG9"
        let payload: [String: Any] = ["channel": channel, "text": message]

        guard let url = URL(string: "https://slack.com/api/chat.postMessage") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        request.timeoutInterval = 10

        _ = try? await URLSession.shared.data(for: request)
    }

    // MARK: - API Helpers

    func getLatestReport() -> HealthReport? {
        return latestReport
    }

    func getReports(periodType: String = "hourly", limit: Int = 24) -> [HealthReport] {
        var reports: [HealthReport] = []
        queue.sync { [weak self] in
            guard let self = self, let db = self.db else { return }
            let sql = "SELECT report_json FROM health_reports WHERE period_type = ? ORDER BY generated_at DESC LIMIT ?"
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return }
            defer { sqlite3_finalize(stmt) }

            sqlite3_bind_text(stmt, 1, (periodType as NSString).utf8String, -1, nil)
            sqlite3_bind_int(stmt, 2, Int32(min(limit, 100)))

            while sqlite3_step(stmt) == SQLITE_ROW {
                if let jsonStr = sqlite3_column_text(stmt, 0),
                   let data = String(cString: jsonStr).data(using: .utf8),
                   let report = try? JSONDecoder().decode(HealthReport.self, from: data) {
                    reports.append(report)
                }
            }
        }
        return reports
    }
}
