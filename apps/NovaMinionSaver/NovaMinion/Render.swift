//
//  Render.swift
//  Nova Minion screensaver
//
//  Pre-rendering helpers. Everything expensive (glows, text, vignette, dust)
//  is drawn once into a CGImage and handed to a CALayer as `contents`, so the
//  per-frame work is just a handful of property changes the GPU composites.
//

import AppKit
import QuartzCore

enum Palette {
    static let space = CGColor(red: 0.012, green: 0.014, blue: 0.030, alpha: 1)
    /// Stars learned on this Mac: warm amber.
    static let warm = NSColor(calibratedRed: 1.00, green: 0.74, blue: 0.42, alpha: 1)
    /// Stars learned elsewhere in the fleet: cool blue-white.
    static let cool = NSColor(calibratedRed: 0.62, green: 0.78, blue: 1.00, alpha: 1)
    static let text = NSColor(calibratedRed: 0.93, green: 0.92, blue: 0.89, alpha: 1)
    static let terminal = NSColor(calibratedRed: 0.62, green: 0.86, blue: 0.80, alpha: 1)
}

enum Render {
    /// Draws into a premultiplied RGBA bitmap of `size` points at `scale`, origin bottom-left.
    static func image(size: CGSize, scale: CGFloat, _ draw: (CGContext) -> Void) -> CGImage? {
        let w = max(1, Int(ceil(size.width * scale))), h = max(1, Int(ceil(size.height * scale)))
        guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
        ctx.scaleBy(x: scale, y: scale)
        let prev = NSGraphicsContext.current
        NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: false)
        draw(ctx)
        NSGraphicsContext.current = prev
        return ctx.makeImage()
    }

    /// Soft radial glow with a bright core — one sprite per colour, reused by every star.
    static func starSprite(color: NSColor, scale: CGFloat) -> CGImage? {
        let d: CGFloat = 32
        return image(size: CGSize(width: d, height: d), scale: scale) { ctx in
            let c = color.usingColorSpace(.sRGB) ?? color
            let comps: [CGFloat] = [
                1, 1, 1, 1,
                c.redComponent, c.greenComponent, c.blueComponent, 0.85,
                c.redComponent, c.greenComponent, c.blueComponent, 0.28,
                c.redComponent, c.greenComponent, c.blueComponent, 0.0,
            ]
            let g = CGGradient(colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!, colorComponents: comps,
                               locations: [0, 0.11, 0.30, 1], count: 4)!
            ctx.drawRadialGradient(g, startCenter: CGPoint(x: d / 2, y: d / 2), startRadius: 0,
                                   endCenter: CGPoint(x: d / 2, y: d / 2), endRadius: d / 2, options: [])
        }
    }

    /// Horizontal streak: transparent tail on the left, bright head on the right.
    static func streakSprite(scale: CGFloat) -> CGImage? {
        let size = CGSize(width: 256, height: 6)
        return image(size: size, scale: scale) { ctx in
            let comps: [CGFloat] = [1, 1, 1, 0, 0.9, 0.95, 1, 0.25, 1, 1, 1, 0.95]
            let g = CGGradient(colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!, colorComponents: comps,
                               locations: [0, 0.7, 1], count: 3)!
            let path = CGPath(roundedRect: CGRect(x: 0, y: 1.5, width: size.width, height: 3),
                              cornerWidth: 1.5, cornerHeight: 1.5, transform: nil)
            ctx.addPath(path)
            ctx.clip()
            ctx.drawLinearGradient(g, start: .zero, end: CGPoint(x: size.width, y: 0), options: [])
        }
    }

    /// Dim background dust for one parallax depth (deterministic per seed), oversized for drift.
    static func dust(size: CGSize, count: Int, seed: UInt64, maxAlpha: CGFloat, maxRadius: CGFloat, scale: CGFloat) -> CGImage? {
        var rng = SeededRandom(seed: seed)
        return image(size: size, scale: scale) { ctx in
            for _ in 0..<count {
                let x = rng.next() * size.width, y = rng.next() * size.height
                let r = 0.3 + rng.next() * maxRadius
                let a = 0.08 + rng.next() * maxAlpha
                let tint = rng.next()
                ctx.setFillColor(CGColor(red: 0.80 + 0.2 * tint, green: 0.85 + 0.1 * tint, blue: 1.0, alpha: a))
                ctx.fillEllipse(in: CGRect(x: x - r, y: y - r, width: 2 * r, height: 2 * r))
            }
        }
    }

    /// Full-screen vignette with an extra darkening pool where the lesson text sits.
    static func vignette(size: CGSize, scale: CGFloat) -> CGImage? {
        // Rendered at a fraction of full resolution; it is a smooth gradient and gets scaled up.
        image(size: size, scale: scale * 0.5) { ctx in
            let space = CGColorSpace(name: CGColorSpace.sRGB)!
            // Smooth (eased) stops: a two-stop linear ramp leaves a visible ring where it starts.
            let edge = smooth(space, alpha: 0.55, from: 0.35, steps: 12)
            let c = CGPoint(x: size.width / 2, y: size.height / 2)
            ctx.drawRadialGradient(edge, startCenter: c, startRadius: 0, endCenter: c,
                                   endRadius: hypot(size.width, size.height) / 2, options: [.drawsAfterEndLocation])
            let pool = smooth(space, alpha: 0.6, from: 0, steps: 12, inverted: true)
            ctx.saveGState()
            ctx.translateBy(x: size.width / 2, y: size.height * 0.24)
            ctx.scaleBy(x: 2.6, y: 1)
            ctx.drawRadialGradient(pool, startCenter: .zero, startRadius: 0, endCenter: .zero,
                                   endRadius: size.height * 0.24, options: [])
            ctx.restoreGState()
        }
    }

    /// Black gradient with smoothstep alpha. Normal: transparent until `from`, then ramps to `alpha` at 1.
    /// Inverted: `alpha` at 0 easing to transparent at 1.
    private static func smooth(_ space: CGColorSpace, alpha: CGFloat, from: CGFloat, steps: Int, inverted: Bool = false) -> CGGradient {
        var comps: [CGFloat] = [], locs: [CGFloat] = []
        for i in 0...steps {
            let u = CGFloat(i) / CGFloat(steps)
            let e = u * u * (3 - 2 * u)
            let a = inverted ? alpha * (1 - e) : alpha * e
            comps += [0, 0, 0, a]
            locs.append(inverted ? u : from + (1 - from) * u)
        }
        return CGGradient(colorSpace: space, colorComponents: comps, locations: locs, count: locs.count)!
    }

    /// Renders an attributed string wrapped to `width`; returns the image and its point size.
    /// Measures and draws with the same NSLayoutManager so wrapping can never disagree.
    static func text(_ s: NSAttributedString, width: CGFloat, scale: CGFloat) -> (CGImage, CGSize)? {
        let storage = NSTextStorage(attributedString: s)
        let lm = NSLayoutManager()
        let tc = NSTextContainer(size: CGSize(width: width, height: .greatestFiniteMagnitude))
        tc.lineFragmentPadding = 0
        lm.addTextContainer(tc)
        storage.addLayoutManager(lm)
        lm.ensureLayout(for: tc)
        let used = lm.usedRect(for: tc)
        let pad: CGFloat = 4
        let size = CGSize(width: ceil(width) + 2 * pad, height: ceil(used.height) + 2 * pad)
        guard let img = image(size: size, scale: scale, { ctx in
            ctx.translateBy(x: 0, y: size.height)
            ctx.scaleBy(x: 1, y: -1)
            NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: true)
            lm.drawGlyphs(forGlyphRange: lm.glyphRange(for: tc), at: CGPoint(x: pad, y: pad - used.minY))
        }) else { return nil }
        return (img, size)
    }
}

// SeededRandom and the stable hash live in apps/Shared/NovaConstellationCore.swift (shared with Nova Cluster).
