// NovaControl — News Summary Data Reader
// Written by Jordan Koch
// Reads from ~/Library/Application Support/NewsSummary/ (or "News Summary")

import Foundation

actor NewsSummaryReader {
    static let shared = NewsSummaryReader()

    private var appSupportDir: URL? {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        for name in ["NewsSummary", "News Summary"] {
            let url = base.appendingPathComponent(name)
            if FileManager.default.fileExists(atPath: url.path) { return url }
        }
        return nil
    }

    private func load<T: Decodable>(_ filename: String, as type: T.Type) -> T? {
        guard let dir = appSupportDir else { return nil }
        let url = dir.appendingPathComponent(filename)
        guard let data = try? Data(contentsOf: url) else { return nil }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try? decoder.decode(type, from: data)
    }

    func fetchBreaking() -> [NewsArticle] {
        let articles = load("articles.json", as: [NewsArticle].self) ?? []
        return Array(articles.filter { !$0.isRead }.prefix(20))
    }

    func fetchByCategory(_ category: String) -> [NewsArticle] {
        let articles = load("articles.json", as: [NewsArticle].self) ?? []
        return articles.filter { $0.category == category }
    }

    func fetchFavorites() -> [NewsArticle] {
        let articles = load("articles.json", as: [NewsArticle].self) ?? []
        return articles.filter { $0.isFavorite }
    }

    func fetchAll() -> [NewsArticle] {
        return load("articles.json", as: [NewsArticle].self) ?? []
    }
}
