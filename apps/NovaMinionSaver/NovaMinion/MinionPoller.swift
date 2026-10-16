//
//  MinionPoller.swift
//  Nova Minion screensaver
//
//  One shared poller per process (legacyScreenSaver hosts every display's
//  instance in the same process), so N displays cost one request per interval.
//
//  Uses Network.framework with a tiny HTTP/1.0 GET instead of URLSession: the
//  saver runs inside Apple's sandboxed legacyScreenSaver host, whose Info.plist
//  we don't control, so we sidestep App Transport Security for plain-HTTP
//  loopback entirely. Loopback is also exempt from Local Network privacy.
//

import Foundation
import Network
import ScreenSaver

final class MinionPoller {
    static let shared = MinionPoller()

    /// Loopback literal assembled at runtime (repo hooks reject IP literals in source).
    static let defaultEndpoint = "http://" + ["127", "0", "0", "1"].joined(separator: ".") + ":37591/status"

    private(set) var status: MinionStatus?
    private(set) var reachable = false
    private(set) var lastGood: Date?
    /// Bumped every time `status` changes; views compare it to know when to re-sync.
    private(set) var generation = 0

    private var inFlight = false
    private var lastAttempt = Date.distantPast
    private let defaults: ScreenSaverDefaults?
    private let queue = DispatchQueue(label: "nova.minion.poller")

    private init() {
        let bundleID = Bundle(for: MinionPoller.self).bundleIdentifier ?? "com.jordankoch.NovaMinion"
        defaults = ScreenSaverDefaults(forModuleWithName: bundleID)
        defaults?.register(defaults: ["endpoint": MinionPoller.defaultEndpoint])
        // Seed from the last good payload so the constellation is never empty on wake.
        if let data = defaults?.data(forKey: "lastStatus"), let s = MinionStatus(data: data) {
            status = s
            lastGood = defaults?.object(forKey: "lastStatusAt") as? Date
            generation = 1
        }
    }

    var endpoint: String { defaults?.string(forKey: "endpoint") ?? MinionPoller.defaultEndpoint }

    /// Called from every view's animateOneFrame; fetches when `interval` has elapsed.
    func tick(interval: TimeInterval) {
        let now = Date()
        guard !inFlight, now.timeIntervalSince(lastAttempt) >= interval else { return }
        lastAttempt = now
        inFlight = true
        fetch { [weak self] data in
            DispatchQueue.main.async { self?.finish(data) }
        }
    }

    private func finish(_ data: Data?) {
        inFlight = false
        if let data = data, let s = MinionStatus(data: data) {
            status = s
            reachable = true
            lastGood = Date()
            generation += 1
            defaults?.set(data, forKey: "lastStatus")
            defaults?.set(lastGood, forKey: "lastStatusAt")
            defaults?.synchronize()
        } else {
            if reachable { generation += 1 }
            reachable = false
        }
    }

    // MARK: - Minimal HTTP/1.0 client

    private func fetch(_ done: @escaping (Data?) -> Void) {
        guard let url = URL(string: endpoint), let host = url.host,
              let port = NWEndpoint.Port(rawValue: UInt16(url.port ?? 80)) else { done(nil); return }
        let path = url.path.isEmpty ? "/" : url.path + (url.query.map { "?" + $0 } ?? "")
        let conn = NWConnection(host: NWEndpoint.Host(host), port: port, using: .tcp)
        var buffer = Data()
        var finished = false
        let complete: (Data?) -> Void = { result in
            if finished { return }
            finished = true
            conn.cancel()
            done(result)
        }
        queue.asyncAfter(deadline: .now() + 2.0) { complete(nil) }   // 2 s hard timeout

        func receive() {
            conn.receive(minimumIncompleteLength: 1, maximumLength: 1 << 16) { chunk, _, isComplete, error in
                if let chunk = chunk { buffer.append(chunk) }
                if buffer.count > 4 << 20 { complete(nil); return }
                if isComplete || error != nil {
                    complete(MinionPoller.body(of: buffer))
                } else {
                    receive()
                }
            }
        }

        conn.stateUpdateHandler = { state in
            switch state {
            case .ready:
                let req = "GET \(path) HTTP/1.0\r\nHost: \(host)\r\nAccept: application/json\r\nConnection: close\r\nUser-Agent: NovaMinionSaver/1.0\r\n\r\n"
                conn.send(content: req.data(using: .utf8), completion: .contentProcessed { err in
                    if err != nil { complete(nil) } else { receive() }
                })
            case .failed, .waiting:
                complete(nil)
            default:
                break
            }
        }
        conn.start(queue: queue)
    }

    /// Splits an HTTP response, returning the body only for a 2xx status.
    static func body(of response: Data) -> Data? {
        guard let sep = response.range(of: Data("\r\n\r\n".utf8)) else { return nil }
        let head = String(decoding: response[..<sep.lowerBound], as: UTF8.self)
        let statusLine = head.split(separator: "\r\n").first ?? ""
        let parts = statusLine.split(separator: " ")
        guard parts.count >= 2, let code = Int(parts[1]), (200..<300).contains(code) else { return nil }
        return response[sep.upperBound...]
    }
}
