// NovaControl — OneOnOne Data Reader
// Written by Jordan Koch
// Reads from ~/Library/Application Support/OneOnOne/

import Foundation

actor OneOnOneReader {
    static let shared = OneOnOneReader()

    private var appSupportDir: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("OneOnOne")
    }

    private func load<T: Decodable>(_ filename: String, as type: T.Type) -> T? {
        let url = appSupportDir.appendingPathComponent(filename)
        guard let data = try? Data(contentsOf: url) else { return nil }
        let decoder = JSONDecoder()
        // OneOnOne stores dates as Apple reference date (seconds since 2001-01-01),
        // not ISO 8601 strings. Handle both formats gracefully.
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            if let interval = try? container.decode(Double.self) {
                return Date(timeIntervalSinceReferenceDate: interval)
            }
            let string = try container.decode(String.self)
            let iso = ISO8601DateFormatter()
            if let date = iso.date(from: string) { return date }
            throw DecodingError.dataCorruptedError(in: container,
                debugDescription: "Cannot decode date from '\(string)'")
        }
        return try? decoder.decode(type, from: data)
    }

    // OneOnOne syncs via CloudKit — local JSON files may be empty if the app
    // hasn't been run recently. Read-only access only; never write to these files.
    func fetchMeetings() -> [Meeting] {
        load("meetings.json", as: [Meeting].self) ?? []
    }

    func fetchActionItems() -> [ActionItem] {
        fetchMeetings().flatMap { $0.actionItems }
    }

    func fetchPeople() -> [Person] {
        load("people.json", as: [Person].self) ?? []
    }

    // Returns true if the OneOnOne data directory exists (app is installed)
    var isAvailable: Bool {
        FileManager.default.fileExists(atPath: appSupportDir.path)
    }

    // Tip for Nova: if meetings are empty, OneOnOne may need to be opened
    // once to sync its CloudKit data to local JSON files.
    var syncNote: String {
        isAvailable ? "Data syncs from CloudKit — open OneOnOne to refresh local cache" : "OneOnOne not installed"
    }

    func fetchGoals() -> [Goal] {
        return load("goals.json", as: [Goal].self) ?? []
    }
}
