//
//  AIService.swift
//  Nova-NextGen Gateway — Swift Client
//
//  Drop this file into any of Jordan's macOS/iOS apps to route AI queries
//  through the Nova-NextGen unified gateway at localhost:34750.
//
//  Usage:
//    let result = try await AIService.shared.query("Explain this code", taskType: .coding)
//    print(result.response)
//
//    // With shared context
//    try await AIService.shared.writeContext(session: "abc", key: "meeting_notes", value: notes)
//    let result = try await AIService.shared.query("Summarize", session: "abc", contextKeys: ["meeting_notes"])
//
//  Created by Jordan Koch
//  Copyright © 2026 Jordan Koch. All rights reserved.
//

import Foundation

// MARK: - Models

public struct AIQueryRequest: Codable {
    public let query: String
    public let taskType: TaskType
    public let preferredBackend: String?
    public let model: String?
    public let sessionId: String?
    public let contextKeys: [String]
    public let validateWith: Int?
    public let stream: Bool

    public enum TaskType: String, Codable {
        case coding, swift, reasoning, analysis, image, vision, creative, long_context, general, auto
    }

    public init(
        query: String,
        taskType: TaskType = .auto,
        preferredBackend: String? = nil,
        model: String? = nil,
        sessionId: String? = nil,
        contextKeys: [String] = [],
        validateWith: Int? = nil,
        stream: Bool = false
    ) {
        self.query = query
        self.taskType = taskType
        self.preferredBackend = preferredBackend
        self.model = model
        self.sessionId = sessionId
        self.contextKeys = contextKeys
        self.validateWith = validateWith
        self.stream = stream
    }

    enum CodingKeys: String, CodingKey {
        case query
        case taskType = "task_type"
        case preferredBackend = "preferred_backend"
        case model
        case sessionId = "session_id"
        case contextKeys = "context_keys"
        case validateWith = "validate_with"
        case stream
    }
}

public struct AIQueryResponse: Codable {
    public let response: String
    public let backendUsed: String
    public let modelUsed: String?
    public let taskType: String
    public let sessionId: String?
    public let tokensPerSecond: Double?
    public let tokenCount: Int?
    public let validated: Bool
    public let consensusScore: Double?
    public let fallbackUsed: Bool
    public let error: String?

    enum CodingKeys: String, CodingKey {
        case response
        case backendUsed = "backend_used"
        case modelUsed = "model_used"
        case taskType = "task_type"
        case sessionId = "session_id"
        case tokensPerSecond = "tokens_per_second"
        case tokenCount = "token_count"
        case validated
        case consensusScore = "consensus_score"
        case fallbackUsed = "fallback_used"
        case error
    }
}

public struct GatewayStatus: Codable {
    public let status: String
    public let version: String
    public let port: Int
    public let uptimeSeconds: Int
    public let backends: [BackendStatus]
    public let activeSessions: Int
    public let totalQueries: Int

    enum CodingKeys: String, CodingKey {
        case status, version, port
        case uptimeSeconds = "uptime_seconds"
        case backends
        case activeSessions = "active_sessions"
        case totalQueries = "total_queries"
    }
}

public struct BackendStatus: Codable {
    public let name: String
    public let available: Bool
    public let url: String
    public let latencyMs: Double?

    enum CodingKeys: String, CodingKey {
        case name, available, url
        case latencyMs = "latency_ms"
    }
}

// MARK: - Errors

public enum AIServiceError: LocalizedError {
    case gatewayUnavailable
    case backendError(String)
    case decodingError(String)
    case networkError(Error)

    public var errorDescription: String? {
        switch self {
        case .gatewayUnavailable:
            return "Nova-NextGen Gateway is not running. Start it with: cd /Volumes/Data/xcode/Nova-NextGen && ./run.sh"
        case .backendError(let msg):
            return "AI backend error: \(msg)"
        case .decodingError(let msg):
            return "Response decoding error: \(msg)"
        case .networkError(let e):
            return "Network error: \(e.localizedDescription)"
        }
    }
}

// MARK: - AIService

@MainActor
public final class AIService {
    public static let shared = AIService()

    private let baseURL: String
    private let session: URLSession
    private let decoder = JSONDecoder()
    private let encoder = JSONEncoder()

    private init(baseURL: String = "http://localhost:34750") {
        self.baseURL = baseURL
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 120
        config.timeoutIntervalForResource = 300
        self.session = URLSession(configuration: config)
        encoder.keyEncodingStrategy = .convertToSnakeCase
    }

    // MARK: - Query

    public func query(
        _ prompt: String,
        taskType: AIQueryRequest.TaskType = .auto,
        backend: String? = nil,
        model: String? = nil,
        session sessionId: String? = nil,
        contextKeys: [String] = [],
        validate: Bool = false
    ) async throws -> AIQueryResponse {
        let req = AIQueryRequest(
            query: prompt,
            taskType: taskType,
            preferredBackend: backend,
            model: model,
            sessionId: sessionId,
            contextKeys: contextKeys,
            validateWith: validate ? 2 : nil
        )
        return try await post("/api/ai/query", body: req)
    }

    // MARK: - Context

    public func writeContext(session: String, key: String, value: String, ttl: Int? = nil) async throws {
        struct ContextWrite: Codable {
            let session_id: String
            let key: String
            let value: String
            let ttl_seconds: Int?
        }
        let _: [String: String] = try await post(
            "/api/context/write",
            body: ContextWrite(session_id: session, key: key, value: value, ttl_seconds: ttl)
        )
    }

    public func readContext(session: String, key: String) async throws -> String? {
        struct Response: Codable { let value: String? }
        let url = "\(baseURL)/api/context/read?session_id=\(session)&key=\(key)"
        do {
            let data = try await get(url)
            let resp = try decoder.decode(Response.self, from: data)
            return resp.value
        } catch {
            return nil
        }
    }

    // MARK: - Status

    public func status() async throws -> GatewayStatus {
        let data = try await get("\(baseURL)/api/ai/status")
        return try decoder.decode(GatewayStatus.self, from: data)
    }

    public func isAvailable() async -> Bool {
        do {
            let data = try await get("\(baseURL)/health")
            let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            return obj?["status"] as? String == "ok"
        } catch {
            return false
        }
    }

    // MARK: - Internals

    @discardableResult
    private func post<T: Encodable, R: Decodable>(_ path: String, body: T) async throws -> R {
        guard let url = URL(string: "\(baseURL)\(path)") else {
            throw AIServiceError.gatewayUnavailable
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        do {
            request.httpBody = try encoder.encode(body)
        } catch {
            throw AIServiceError.decodingError("Encoding failed: \(error)")
        }
        do {
            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
                if let errObj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let detail = errObj["detail"] as? String {
                    throw AIServiceError.backendError(detail)
                }
                throw AIServiceError.backendError("HTTP \(http.statusCode)")
            }
            return try decoder.decode(R.self, from: data)
        } catch let e as AIServiceError { throw e }
        catch let urlError as URLError where urlError.code == .cannotConnectToHost {
            throw AIServiceError.gatewayUnavailable
        }
        catch { throw AIServiceError.networkError(error) }
    }

    private func get(_ urlString: String) async throws -> Data {
        guard let url = URL(string: urlString) else { throw AIServiceError.gatewayUnavailable }
        do {
            let (data, _) = try await session.data(from: url)
            return data
        } catch let urlError as URLError where urlError.code == .cannotConnectToHost {
            throw AIServiceError.gatewayUnavailable
        }
        catch { throw AIServiceError.networkError(error) }
    }
}
