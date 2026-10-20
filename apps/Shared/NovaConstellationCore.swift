//
//  NovaConstellationCore.swift
//  Shared by Nova Cluster (Mac / iPad / Apple TV) and the Nova Minion screensaver.
//
//  The pure, platform-free half of Nova's constellation: stable hashing, a seeded
//  random stream, lesson-text cleanup, and where each lesson's star sits in the sky.
//  Rendering stays with each target (SwiftUI Canvas in the app, Core Animation in
//  the screensaver), so both skies place the same lesson at the same spot.
//

import CoreGraphics
import Foundation

/// Stable (per-string, across launches) hash so layouts stay put between polls and restarts.
/// Swift's Hasher is randomly seeded per process, so it can't be used for this.
enum StableHash {
    /// Raw 64-bit FNV-1a.
    static func fnv1a(_ s: String) -> UInt64 {
        var h: UInt64 = 0xcbf2_9ce4_8422_2325
        for b in s.utf8 { h ^= UInt64(b); h = h &* 0x100_0000_01b3 }
        return h
    }

    /// FNV-1a with a seed, then a finalizer so nearby strings spread across the range.
    static func of(_ s: String, seed: UInt64 = 0) -> UInt64 {
        var h: UInt64 = 0xcbf2_9ce4_8422_2325 ^ seed
        for b in s.utf8 { h ^= UInt64(b); h = h &* 0x100_0000_01b3 }
        h ^= h >> 33; h = h &* 0xff51_afd7_ed55_8ccd; h ^= h >> 33
        return h
    }

    /// Uniform in [0, 1).
    static func unit(_ s: String, _ seed: UInt64) -> Double {
        Double(of(s, seed: seed) >> 11) / Double(1 << 53)
    }
}

/// SplitMix64: a small, fast, seeded stream (same seed, same numbers, on every platform).
struct SeededRandom {
    private var state: UInt64
    init(seed: UInt64) { state = seed &+ 0x9E37_79B9_7F4A_7C15 }
    mutating func nextUInt() -> UInt64 {
        state = state &+ 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        return z ^ (z >> 31)
    }
    /// Uniform in [0, 1).
    mutating func next() -> CGFloat { CGFloat(nextUInt() >> 11) / CGFloat(1 << 53) }
    /// Approximately normal (mean 0, sd ~1).
    mutating func gauss() -> CGFloat { (next() + next() + next() + next() - 2) * 1.73 }
}

/// Lesson text as the hub writes it can carry citation markers like "[3][12]" or "[3, 4]"; strip them.
enum LessonText {
    private static let citations = try! NSRegularExpression(pattern: #"\s*\[\d+(?:\s*[,–-]\s*\d+)*\]"#)
    static func clean(_ s: String) -> String {
        let r = NSRange(s.startIndex..., in: s)
        let stripped = citations.stringByReplacingMatches(in: s, range: r, withTemplate: "")
        return stripped.split(whereSeparator: \.isWhitespace).joined(separator: " ")
            .replacingOccurrences(of: " .", with: ".")
            .replacingOccurrences(of: " ,", with: ",")
    }
}

/// Where a lesson's star sits. Lessons from the same work cluster around one centre;
/// works are spread over three parallax depths.
enum ConstellationLayout {
    struct Placement: Equatable {
        /// 0…1 in field space (y grows downward in the app, upward in the screensaver; both draw it the same way).
        var norm: CGPoint
        var depth: Int
        /// 0…1, for the star's size.
        var sizeJitter: CGFloat
        /// For choosing a twinkle group.
        var twinkle: UInt64
    }

    /// `aspect` is the field's width / height. The defaults keep stars below the top band
    /// (the screensaver's terminal / the app's minion cards) and above the lesson text.
    static func place(work: String, key: String, aspect: CGFloat,
                      yCenterMin: CGFloat = 0.34, yCenterSpan: CGFloat = 0.48) -> Placement {
        let workKey = work.lowercased()
        let wh = StableHash.fnv1a(workKey)
        var wr = SeededRandom(seed: wh)
        let cx = 0.07 + wr.next() * 0.86
        let cy = yCenterMin + wr.next() * yCenterSpan
        var sr = SeededRandom(seed: StableHash.fnv1a(key))
        let nx = min(0.98, max(0.02, cx + sr.gauss() * 0.032 / max(aspect / 1.78, 0.5)))
        let ny = min(0.87, max(0.06, cy + sr.gauss() * 0.05))
        let jitter = sr.next()
        let twinkle = sr.nextUInt()
        return Placement(norm: CGPoint(x: nx, y: ny), depth: Int(wh % 3), sizeJitter: jitter, twinkle: twinkle)
    }

    /// Index pairs that join stars of the same work, in arrival order (`works` is oldest-first).
    static func links(works: [String]) -> [(Int, Int)] {
        var last: [String: Int] = [:]
        var out: [(Int, Int)] = []
        for (i, w) in works.enumerated() {
            let k = w.lowercased()
            if let j = last[k] { out.append((j, i)) }
            last[k] = i
        }
        return out
    }
}
