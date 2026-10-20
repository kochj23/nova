//
//  Palette.swift
//  Nova Cluster (iPad + Mac)
//
//  The Nova Minion look (near-black space, amber + cool accents, ultralight
//  wordmark, monospaced data) plus a stable color per model family so the same
//  model reads the same everywhere on the map, in the lists and in the legend.
//

import SwiftUI

enum Palette {
    static let space = Color(red: 0.012, green: 0.014, blue: 0.030)
    static let text = Color(red: 0.93, green: 0.92, blue: 0.89)
    static let terminal = Color(red: 0.62, green: 0.86, blue: 0.80)
    static let warm = Color(red: 1.00, green: 0.74, blue: 0.42)
    static let cool = Color(red: 0.58, green: 0.76, blue: 1.00)
    static let alert = Color(red: 0.95, green: 0.38, blue: 0.40)

    struct RGB: Equatable {
        var r, g, b: Double
        init(_ r: Double, _ g: Double, _ b: Double) { self.r = r; self.g = g; self.b = b }
        func color(_ a: Double = 1) -> Color { Color(red: r, green: g, blue: b, opacity: a) }
        func mixed(toWhite t: Double) -> RGB { RGB(r + (1 - r) * t, g + (1 - g) * t, b + (1 - b) * t) }
    }

    static let amber = RGB(1.00, 0.74, 0.42)
    static let blue = RGB(0.58, 0.76, 1.00)
    static let teal = RGB(0.52, 0.92, 0.78)
    static let rose = RGB(1.00, 0.56, 0.68)
    static let violet = RGB(0.78, 0.64, 1.00)
    static let lime = RGB(0.82, 0.93, 0.50)
    static let gold = RGB(1.00, 0.88, 0.55)
    static let orange = RGB(1.00, 0.60, 0.36)
    static let slate = RGB(0.66, 0.74, 0.82)

    /// Station core tint: the hub is warm white, Macs amber-ish, Linux cool.
    static func station(os: String?, hub: Bool) -> RGB {
        if hub { return RGB(1.00, 0.86, 0.66) }
        return (os ?? "").lowercased().contains("macos") ? RGB(1.00, 0.80, 0.58) : RGB(0.66, 0.80, 1.00)
    }

    /// Minion state ring colors (same as Nova Minion's state dots).
    static func minionState(_ s: String?) -> RGB {
        switch (s ?? "").lowercased() {
        case "dedicated": return RGB(0.42, 0.95, 0.62)
        case "idle": return RGB(1.0, 0.78, 0.36)
        case "away": return RGB(0.55, 0.66, 0.90)
        default: return RGB(0.95, 0.38, 0.40)
        }
    }
}

/// Model name → family → hue. Same family, same color, on every machine.
enum ModelHue {
    static func family(_ model: String) -> String {
        let base = model.split(separator: ":").first.map(String.init) ?? model
        return base.lowercased()
    }
    static func of(_ model: String) -> Palette.RGB {
        let f = family(model)
        if f == "nova" { return Palette.gold }
        if f.contains("embed") { return Palette.slate }
        if f.contains("coder") { return Palette.orange }
        if f.contains("vl") { return Palette.rose }
        if f.hasPrefix("qwen") { return Palette.amber }
        if f.hasPrefix("llama") { return Palette.blue }
        if f.hasPrefix("deepseek") { return Palette.violet }
        if f.contains("whisper") { return Palette.teal }
        let pool = [Palette.teal, Palette.lime, Palette.violet, Palette.blue, Palette.rose]
        return pool[Int(StableHash.of(f) % UInt64(pool.count))]
    }
    static func short(_ model: String) -> String {
        model.hasSuffix(":latest") ? String(model.dropLast(7)) : model
    }
}

// StableHash and LessonText live in apps/Shared/NovaConstellationCore.swift (shared with the screensaver).

enum Fmt {
    nonisolated(unsafe) private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime]; return f
    }()
    nonisolated(unsafe) private static let isoFrac: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f
    }()
    static func date(_ s: String?) -> Date? {
        guard let s else { return nil }
        return iso.date(from: s) ?? isoFrac.date(from: s)
    }
    static func clock(_ d: Date?) -> String {
        guard let d else { return "" }
        return d.formatted(date: .omitted, time: .shortened)
    }
    static func int(_ n: Int) -> String { n.formatted(.number.grouping(.automatic)) }
    static func int(_ n: Double?) -> String { int(Int((n ?? 0).rounded())) }
    static func one(_ x: Double?) -> String { (x ?? 0).formatted(.number.precision(.fractionLength(1))) }
    static func gb(_ x: Double?) -> String {
        let v = x ?? 0
        return v >= 100 ? int(v) : one(v)
    }
    /// 9,029 · 184.2k · 3.41M
    static func compact(_ x: Double?) -> String {
        let v = x ?? 0
        if v < 100_000 { return int(v) }
        if v < 10_000_000 { return (v / 1000).formatted(.number.precision(.fractionLength(v < 1_000_000 ? 1 : 0))) + "k" }
        return (v / 1_000_000).formatted(.number.precision(.fractionLength(2))) + "M"
    }
    static func plural(_ n: Int, _ one: String, _ many: String) -> String { "\(int(n)) \(n == 1 ? one : many)" }
    /// "12s", "4m", "2h", "3d".
    static func ago(_ s: Double) -> String {
        let v = max(0, s)
        if v < 90 { return "\(Int(v))s" }
        if v < 5400 { return "\(Int(v / 60))m" }
        if v < 172800 { return "\(Int(v / 3600))h" }
        return "\(Int(v / 86400))d"
    }
    /// "Apple M4 Pro" → "M4 Pro"; "AMD Ryzen AI 9 HX 470 w/ Radeon 890M" → "Ryzen AI 9 HX 470".
    static func chip(_ c: String?) -> String {
        var s = c ?? ""
        if let r = s.range(of: " w/ ") { s = String(s[..<r.lowerBound]) }
        for p in ["Apple ", "AMD ", "Intel "] where s.hasPrefix(p) { s = String(s.dropFirst(p.count)) }
        return s
    }
    /// "macOS 26.5.2" → "macOS 26.5"; "Ubuntu 26.04.1" → "Ubuntu 26.04".
    static func os(_ o: String?) -> String {
        let parts = (o ?? "").split(separator: " ")
        guard let last = parts.last, last.first?.isNumber == true else { return o ?? "" }
        let v = last.split(separator: ".").prefix(2).joined(separator: ".")
        return (parts.dropLast().map(String.init) + [v]).joined(separator: " ")
    }
}

/// Faint static dust, drawn once per size change (not per frame).
struct DustLayer: View {
    var body: some View {
        Canvas(rendersAsynchronously: true) { ctx, size in
            let n = Int((size.width * size.height) / 3200)
            for i in 0..<n {
                let s = "dust\(i)"
                let x = StableHash.unit(s, 11) * size.width
                let y = StableHash.unit(s, 12) * size.height
                let a = 0.05 + 0.20 * pow(StableHash.unit(s, 13), 2)
                let r = 0.4 + 0.7 * StableHash.unit(s, 14)
                ctx.fill(Path(ellipseIn: CGRect(x: x, y: y, width: r * 2, height: r * 2)),
                         with: .color(Color(red: 0.80, green: 0.85, blue: 1.0, opacity: a)))
            }
        }
        .allowsHitTesting(false)
    }
}
