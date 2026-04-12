// NovaControl — MLXCode Reader
// Written by Jordan Koch
// Proxies the MLXCode local HTTP API on port 37422

import Foundation

actor MLXCodeReader {
    static let shared = MLXCodeReader()

    private let baseURL = URL(string: "http://127.0.0.1:37422")!

    func fetchStatus() async -> MLXCodeInfo? {
        guard let url = URL(string: "http://127.0.0.1:37422/api/status") else { return nil }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.0
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse, http.statusCode == 200,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return MLXCodeInfo(
            status:      (json["status"] as? String) ?? "online",
            activeModel: json["model"]       as? String,
            queueDepth:  json["queueDepth"]  as? Int
        )
    }

    /// Proxy a GET request to MLXCode and return the raw JSON response body
    func proxy(path: String) async -> (statusCode: Int, body: Any) {
        guard let url = URL(string: "http://127.0.0.1:37422\(path)") else {
            return (400, ["error": "invalid path"])
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 5.0
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse else {
            return (503, ["error": "MLXCode unreachable", "port": 37422])
        }
        let body = (try? JSONSerialization.jsonObject(with: data)) ?? ["error": "non-JSON response"]
        return (http.statusCode, body)
    }
}
