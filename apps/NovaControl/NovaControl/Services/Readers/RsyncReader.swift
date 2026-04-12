// NovaControl — RsyncGUI Data Reader
// Written by Jordan Koch
// Reads from ~/Library/Application Support/RsyncGUI/

import Foundation

actor RsyncReader {
    static let shared = RsyncReader()

    private var appSupportDir: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("RsyncGUI")
    }

    func fetchJobs() -> [SyncJob] {
        let url = appSupportDir.appendingPathComponent("jobs.json")
        guard let data = try? Data(contentsOf: url) else { return [] }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return (try? decoder.decode([SyncJob].self, from: data)) ?? []
    }

    func fetchHistory() -> [ExecutionHistoryEntry] {
        let url = appSupportDir.appendingPathComponent("History/history.json")
        guard let data = try? Data(contentsOf: url) else { return [] }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let entries = (try? decoder.decode([ExecutionHistoryEntry].self, from: data)) ?? []
        return Array(entries.prefix(50))
    }

    func runJob(_ jobId: UUID) async -> String {
        let jobs = fetchJobs()
        guard let job = jobs.first(where: { $0.id == jobId }) else {
            return "Job not found: \(jobId)"
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/rsync")
        var args = ["-av", "--progress"]
        args.append(contentsOf: job.sources)
        args.append(job.destination)
        process.arguments = args
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return "rsync launch failed: \(error.localizedDescription)"
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8) ?? "Done"
    }
}
