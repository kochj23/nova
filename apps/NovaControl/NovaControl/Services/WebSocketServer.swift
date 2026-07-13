// NovaControl — WebSocket Event Stream
// Written by Jordan Koch
// Provides /ws endpoint for real-time state change events.
// Eliminates polling for connected dashboards.

import Foundation
import Network
import CryptoKit

// MARK: - WebSocket Event

struct WSEvent: Codable {
    let type: String        // "status_change", "alert", "metric", "workflow_run", "audit"
    let timestamp: String
    let payload: [String: String]

    init(type: String, payload: [String: String]) {
        self.type = type
        self.timestamp = ISO8601DateFormatter().string(from: Date())
        self.payload = payload
    }

    var jsonData: Data? {
        try? JSONEncoder().encode(self)
    }
}

// MARK: - WebSocket Connection

final class WSConnection: Identifiable {
    let id: UUID
    let connection: NWConnection
    var subscribedEvents: Set<String>  // Empty = all events
    var isAlive: Bool = true

    init(connection: NWConnection, subscribedEvents: Set<String> = []) {
        self.id = UUID()
        self.connection = connection
        self.subscribedEvents = subscribedEvents
    }
}

// MARK: - WebSocket Server

final class WebSocketServer {
    static let shared = WebSocketServer()

    private var connections: [UUID: WSConnection] = [:]
    private let queue = DispatchQueue(label: "net.digitalnoise.novacontrol.websocket", qos: .utility)
    private var pingTimer: DispatchSourceTimer?

    private init() {
        startPingTimer()
    }

    // MARK: - Connection Management

    /// Handle the WebSocket upgrade from the HTTP server.
    /// This is called when the server detects a WebSocket upgrade request.
    func handleUpgrade(connection: NWConnection, requestHeaders: String) {
        // Extract Sec-WebSocket-Key from headers
        guard let wsKey = extractHeader("sec-websocket-key", from: requestHeaders) else {
            NSLog("[WebSocket] Missing Sec-WebSocket-Key header")
            connection.cancel()
            return
        }

        // Compute accept key per RFC 6455
        let magicString = "258EAFA5-E914-47DA-95CA-5AB5DC76B45E"
        let acceptInput = wsKey + magicString
        let hash = Insecure.SHA1.hash(data: Data(acceptInput.utf8))
        let acceptKey = Data(hash).base64EncodedString()

        // Send upgrade response
        let response = [
            "HTTP/1.1 101 Switching Protocols",
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Accept: \(acceptKey)",
            "",
            ""
        ].joined(separator: "\r\n")

        guard let responseData = response.data(using: .utf8) else {
            connection.cancel()
            return
        }

        connection.send(content: responseData, completion: .contentProcessed { [weak self] error in
            if let error = error {
                NSLog("[WebSocket] Upgrade response send error: \(error)")
                connection.cancel()
                return
            }
            // Connection is now a WebSocket
            let wsConn = WSConnection(connection: connection)
            self?.queue.async {
                self?.connections[wsConn.id] = wsConn
                NSLog("[WebSocket] Client connected (id: \(wsConn.id), total: \(self?.connections.count ?? 0))")
            }
            self?.startReading(wsConn)

            // Send welcome event
            let welcome = WSEvent(type: "connected", payload: [
                "server": "NovaControl",
                "version": "1.1.0",
                "connectionId": wsConn.id.uuidString
            ])
            self?.sendToConnection(wsConn, event: welcome)
        })
    }

    // MARK: - Broadcasting

    /// Broadcast an event to all connected clients (filtered by subscription).
    func broadcast(_ event: WSEvent) {
        queue.async { [weak self] in
            guard let self = self else { return }
            for (_, conn) in self.connections where conn.isAlive {
                // Check subscription filter
                if !conn.subscribedEvents.isEmpty && !conn.subscribedEvents.contains(event.type) {
                    continue
                }
                self.sendToConnection(conn, event: event)
            }
        }
    }

    /// Convenience: broadcast a status change event.
    func broadcastStatusChange(serviceId: String, oldStatus: String, newStatus: String) {
        let event = WSEvent(type: "status_change", payload: [
            "serviceId": serviceId,
            "oldStatus": oldStatus,
            "newStatus": newStatus
        ])
        broadcast(event)
    }

    /// Convenience: broadcast a metric update.
    func broadcastMetric(name: String, value: String) {
        let event = WSEvent(type: "metric", payload: ["name": name, "value": value])
        broadcast(event)
    }

    /// Convenience: broadcast an alert event.
    func broadcastAlert(ruleId: String, message: String, escalated: Bool) {
        let event = WSEvent(type: "alert", payload: [
            "ruleId": ruleId,
            "message": message,
            "escalated": escalated ? "true" : "false"
        ])
        broadcast(event)
    }

    /// Current connection count.
    var connectionCount: Int {
        queue.sync { connections.count }
    }

    // MARK: - WebSocket Frame Handling

    private func sendToConnection(_ conn: WSConnection, event: WSEvent) {
        guard let jsonData = event.jsonData else { return }
        let frame = encodeTextFrame(jsonData)
        conn.connection.send(content: frame, completion: .contentProcessed { error in
            if error != nil {
                conn.isAlive = false
            }
        })
    }

    private func startReading(_ conn: WSConnection) {
        conn.connection.receive(minimumIncompleteLength: 2, maximumLength: 65536) { [weak self] data, _, isComplete, error in
            guard let self = self else { return }

            if let data = data, !data.isEmpty {
                self.processFrame(data, from: conn)
            }

            if isComplete || error != nil {
                self.removeConnection(conn.id)
                return
            }

            // Continue reading
            self.startReading(conn)
        }
    }

    private func processFrame(_ data: Data, from conn: WSConnection) {
        guard data.count >= 2 else { return }
        let opcode = data[0] & 0x0F

        switch opcode {
        case 0x08: // Close
            removeConnection(conn.id)
        case 0x09: // Ping
            sendPong(to: conn, data: data)
        case 0x0A: // Pong
            conn.isAlive = true
        case 0x01: // Text frame
            if let payload = decodeTextFrame(data) {
                handleClientMessage(payload, from: conn)
            }
        default:
            break
        }
    }

    private func handleClientMessage(_ message: String, from conn: WSConnection) {
        // Clients can send subscription filters: {"subscribe": ["status_change", "alert"]}
        guard let data = message.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }

        if let subscribe = json["subscribe"] as? [String] {
            queue.async {
                conn.subscribedEvents = Set(subscribe)
                NSLog("[WebSocket] Client \(conn.id) subscribed to: \(subscribe)")
            }
        }

        if let unsubscribe = json["unsubscribe"] as? [String] {
            queue.async {
                conn.subscribedEvents.subtract(unsubscribe)
            }
        }
    }

    // MARK: - WebSocket Frame Encoding/Decoding

    private func encodeTextFrame(_ payload: Data) -> Data {
        var frame = Data()
        frame.append(0x81) // FIN + Text opcode

        if payload.count < 126 {
            frame.append(UInt8(payload.count))
        } else if payload.count <= 65535 {
            frame.append(126)
            frame.append(UInt8((payload.count >> 8) & 0xFF))
            frame.append(UInt8(payload.count & 0xFF))
        } else {
            frame.append(127)
            for i in (0..<8).reversed() {
                frame.append(UInt8((payload.count >> (i * 8)) & 0xFF))
            }
        }

        frame.append(payload)
        return frame
    }

    private func decodeTextFrame(_ data: Data) -> String? {
        guard data.count >= 2 else { return nil }
        let masked = (data[1] & 0x80) != 0
        var payloadLength = UInt64(data[1] & 0x7F)
        var offset = 2

        if payloadLength == 126 {
            guard data.count >= 4 else { return nil }
            payloadLength = UInt64(data[2]) << 8 | UInt64(data[3])
            offset = 4
        } else if payloadLength == 127 {
            guard data.count >= 10 else { return nil }
            payloadLength = 0
            for i in 0..<8 {
                payloadLength |= UInt64(data[2 + i]) << UInt64((7 - i) * 8)
            }
            offset = 10
        }

        var maskKey: [UInt8] = []
        if masked {
            guard data.count >= offset + 4 else { return nil }
            maskKey = [data[offset], data[offset+1], data[offset+2], data[offset+3]]
            offset += 4
        }

        let end = offset + Int(payloadLength)
        guard data.count >= end else { return nil }
        var payload = Data(data[offset..<end])

        if masked {
            for i in 0..<payload.count {
                payload[i] ^= maskKey[i % 4]
            }
        }

        return String(data: payload, encoding: .utf8)
    }

    private func sendPong(to conn: WSConnection, data: Data) {
        // Pong frame mirrors ping payload
        var pong = Data()
        pong.append(0x8A) // FIN + Pong
        pong.append(0x00) // No payload (or mirror ping data)
        conn.connection.send(content: pong, completion: .contentProcessed { _ in })
    }

    // MARK: - Ping/Keepalive

    private func startPingTimer() {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 30, repeating: 30)
        timer.setEventHandler { [weak self] in
            self?.pingAll()
        }
        timer.resume()
        pingTimer = timer
    }

    private func pingAll() {
        var deadConnections: [UUID] = []

        for (id, conn) in connections {
            if !conn.isAlive {
                deadConnections.append(id)
                continue
            }
            // Mark as potentially dead; pong response sets isAlive back to true
            conn.isAlive = false
            var ping = Data()
            ping.append(0x89) // FIN + Ping
            ping.append(0x00)
            conn.connection.send(content: ping, completion: .contentProcessed { _ in })
        }

        for id in deadConnections {
            removeConnection(id)
        }
    }

    private func removeConnection(_ id: UUID) {
        queue.async { [weak self] in
            if let conn = self?.connections.removeValue(forKey: id) {
                conn.connection.cancel()
                NSLog("[WebSocket] Client disconnected (id: \(id), remaining: \(self?.connections.count ?? 0))")
            }
        }
    }

    // MARK: - Helper

    private func extractHeader(_ name: String, from headers: String) -> String? {
        for line in headers.components(separatedBy: "\r\n") {
            guard let colonIdx = line.firstIndex(of: ":") else { continue }
            let key = String(line[..<colonIdx]).lowercased().trimmingCharacters(in: .whitespaces)
            if key == name {
                return String(line[line.index(after: colonIdx)...]).trimmingCharacters(in: .whitespaces)
            }
        }
        return nil
    }
}
