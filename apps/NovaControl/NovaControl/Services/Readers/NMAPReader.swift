// NovaControl — NMAPScanner Data Reader
// Written by Jordan Koch
// Reads NMAPScanner data from UserDefaults and runs live scans

import Foundation

actor NMAPReader {
    static let shared = NMAPReader()

    // NMAPScanner is sandboxed — its UserDefaults live in its container plist, not standard UserDefaults.
    // Read directly from the plist file.
    private var nmapPlist: URL {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return home
            .appendingPathComponent("Library/Containers/com.digitalnoise.nmapscanner.macos/Data/Library/Preferences/com.digitalnoise.nmapscanner.macos.plist")
    }

    private func readFromPlist(key: String) -> Data? {
        guard let dict = NSDictionary(contentsOf: nmapPlist),
              let data = dict[key] as? Data else { return nil }
        return data
    }

    // NMAPScanner stores dates as Apple's timeIntervalSinceReferenceDate (Double)
    private var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .secondsSince1970
        return d
    }

    func fetchDevices() -> [ScannedDevice] {
        guard let data = readFromPlist(key: "com.digitalnoise.nmapscanner.devices") else { return [] }
        return (try? decoder.decode([ScannedDevice].self, from: data)) ?? []
    }

    func fetchThreats() -> [ThreatFinding] {
        guard let data = readFromPlist(key: "com.digitalnoise.nmapscanner.threats") else { return [] }
        return (try? decoder.decode([ThreatFinding].self, from: data)) ?? []
    }

    func runScan(ip: String) async -> String {
        // Run nmap directly — requires nmap installed via homebrew
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["nmap", "-sn", ip]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return "nmap launch failed: \(error.localizedDescription)"
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8) ?? "Scan failed"
    }
}
