//
//  NovaMinionView.swift
//  Nova Minion screensaver
//
//  Shows what this Mac is doing for Nova while idle: a drifting constellation
//  of learned lessons, the latest lesson in large serif type, a typed-out
//  "now reading" terminal, and a quiet status line.
//
//  Everything is layer-composited; animateOneFrame only nudges a few layer
//  properties and re-renders text images when their content actually changes.
//

import AppKit
import QuartzCore
import ScreenSaver

@objc(NovaMinionView)
final class NovaMinionView: ScreenSaverView {
    private let constellation = Constellation()
    private let vignette = CALayer()
    private let lessonLayers = [CALayer(), CALayer()]
    private let terminal = CALayer()
    private let cursor = CALayer()
    private let statusLine = CALayer()
    private let wordmark = CALayer()

    private var unit: CGFloat = 1          // layout unit: 1 pt at 1080p
    private var textUnit: CGFloat = 1      // larger in the System Settings preview
    private var backing: CGFloat = 2
    private var seenGeneration = -1
    private var paused = false
    private var startedAt = CACurrentMediaTime()

    // Lesson rotation
    private var priority: [String] = []        // new lessons shown first
    private var rotation: [MinionLesson] = []
    private var rotationIndex = 0
    private var shownKey: String?
    private var activeLesson = 0               // which of lessonLayers is front
    private var lastSwap: CFTimeInterval = -100
    private var lessonHeights: [CGFloat] = [0, 0]
    private let rotateEvery: CFTimeInterval = 12

    // Terminal typing
    private var termTarget = ""
    private var termTyped = 0
    private var termRendered = -1
    private var lastTypeTick: CFTimeInterval = 0
    private var termLastChange: CFTimeInterval = 0
    private var monoAdvance: CGFloat = 8
    private var termLineHeight: CGFloat = 18

    private var statusText = ""
    private var lastStatusRefresh: CFTimeInterval = 0

    // MARK: - Lifecycle

    override init?(frame: NSRect, isPreview: Bool) {
        super.init(frame: frame, isPreview: isPreview)
        animationTimeInterval = 1.0 / 30.0
        wantsLayer = true
        layerContentsRedrawPolicy = .never
        buildLayers()
        // Sonoma+ legacyScreenSaver keeps instances alive after the saver is dismissed;
        // pause on willstop so orphans stop polling and animating.
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(willStop),
                                                            name: Notification.Name("com.apple.screensaver.willstop"), object: nil)
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        animationTimeInterval = 1.0 / 30.0
        wantsLayer = true
        buildLayers()
    }

    deinit { DistributedNotificationCenter.default().removeObserver(self) }

    @objc private func willStop() { paused = true }

    override var hasConfigureSheet: Bool { false }
    override var configureSheet: NSWindow? { nil }
    override var isOpaque: Bool { true }

    override func startAnimation() {
        super.startAnimation()
        paused = false
    }

    override func stopAnimation() {
        super.stopAnimation()
        paused = true
    }

    override func draw(_ rect: NSRect) {
        NSColor(cgColor: Palette.space)?.setFill()
        rect.fill()
    }

    private func buildLayers() {
        guard let root = layer else { return }
        root.backgroundColor = Palette.space
        root.addSublayer(constellation.root)
        root.addSublayer(vignette)
        for l in lessonLayers { l.opacity = 0; root.addSublayer(l) }
        terminal.anchorPoint = CGPoint(x: 0, y: 1)
        root.addSublayer(terminal)
        cursor.backgroundColor = Palette.terminal.withAlphaComponent(0.85).cgColor
        root.addSublayer(cursor)
        statusLine.anchorPoint = CGPoint(x: 0.5, y: 0)
        root.addSublayer(statusLine)
        wordmark.anchorPoint = CGPoint(x: 1, y: 1)
        root.addSublayer(wordmark)
        relayout()
    }

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        relayout()
    }

    override func viewDidChangeBackingProperties() {
        super.viewDidChangeBackingProperties()
        relayout()
    }

    private func relayout() {
        let size = bounds.size
        guard size.width > 1, size.height > 1, let root = layer else { return }
        backing = window?.backingScaleFactor ?? NSScreen.main?.backingScaleFactor ?? 2
        unit = size.height / 1080
        textUnit = isPreview ? max(unit * 2.0, 0.3) : unit
        constellation.layout(size: size, unit: unit, scale: backing)
        noActions {
            root.contentsScale = backing
            vignette.frame = root.bounds
            vignette.contents = Render.vignette(size: size, scale: backing)
            terminal.contentsScale = backing
            statusLine.contentsScale = backing
            wordmark.contentsScale = backing
            for l in lessonLayers { l.contentsScale = backing }
            renderWordmark()
            statusText = ""
            termRendered = -1
            terminal.isHidden = isPreview
            cursor.isHidden = isPreview
            statusLine.isHidden = isPreview && size.width < 500
        }
        let mono = NSFont.monospacedSystemFont(ofSize: 13 * textUnit, weight: .regular)
        monoAdvance = ("M" as NSString).size(withAttributes: [.font: mono]).width
        termLineHeight = ceil(20 * textUnit)
        // Re-render the visible lesson at the new size.
        if let key = shownKey, let lesson = rotation.first(where: { $0.key == key }) {
            renderLesson(lesson, into: activeLesson)
        }
    }

    // MARK: - Frame

    override func animateOneFrame() {
        if paused { return }
        let t = CACurrentMediaTime()
        let poller = MinionPoller.shared
        poller.tick(interval: isPreview ? 30 : 5)
        if poller.generation != seenGeneration {
            seenGeneration = poller.generation
            sync(poller.status, now: t)
        }
        constellation.update(now: t)
        noActions {
            updateLesson(t)
            updateTerminal(t)
            if t - lastStatusRefresh > 1 { lastStatusRefresh = t; updateStatusLine() }
            let breath = 0.5 + 0.5 * sin((t - startedAt) * 2 * .pi / 9.0)
            wordmark.opacity = Float(0.30 + 0.32 * breath)
        }
    }

    // MARK: - Data → visuals

    private func sync(_ status: MinionStatus?, now: CFTimeInterval) {
        guard let status = status else { updateTerminalTarget(nil); return }
        let lessons = status.recentLessons               // newest first per the contract
        let initial = constellation.count == 0
        let known = Set(rotation.map { $0.key })
        constellation.ingest(lessons, initial: initial, now: now)
        if !initial {
            // Newly arrived lessons jump the rotation queue (newest last so it shows first… after older new ones).
            for l in lessons.reversed() where !known.contains(l.key) && !priority.contains(l.key) { priority.append(l.key) }
        }
        if !lessons.isEmpty { rotation = Array(lessons.prefix(120)) }
        updateTerminalTarget(status)
        statusText = ""   // force refresh
        if shownKey == nil, let first = rotation.first {
            // Show the newest lesson right away, after the stars have begun to appear.
            lastSwap = now - rotateEvery + 1.2
            priority.removeAll { $0 == first.key }
            priority.insert(first.key, at: 0)
        }
    }

    private func nextLesson() -> MinionLesson? {
        while let k = priority.first {
            priority.removeFirst()
            if let l = rotation.first(where: { $0.key == k }) { return l }
        }
        guard !rotation.isEmpty else { return nil }
        rotationIndex = (rotationIndex + 1) % rotation.count
        let l = rotation[rotationIndex]
        if l.key == shownKey && rotation.count > 1 {
            rotationIndex = (rotationIndex + 1) % rotation.count
            return rotation[rotationIndex]
        }
        return l
    }

    private func updateLesson(_ t: CFTimeInterval) {
        if t - lastSwap >= rotateEvery, let next = nextLesson() {
            lastSwap = t
            let back = 1 - activeLesson
            renderLesson(next, into: back)
            activeLesson = back
            shownKey = next.key
            constellation.highlight(next.key)
        }
        // Sequential crossfade: old out over 1.1 s, new in from 0.8 s to 2.6 s, rising slightly.
        let dt = t - lastSwap
        let outA = Float(1 - clamp(dt / 1.1))
        let inP = clamp((dt - 0.8) / 1.8)
        let inA = Float(inP * inP * (3 - 2 * inP))
        let front = lessonLayers[activeLesson], back = lessonLayers[1 - activeLesson]
        front.opacity = inA
        back.opacity = min(back.opacity, outA)
        let rise = CGFloat(1 - inA) * 10 * textUnit
        front.position = CGPoint(x: bounds.midX, y: lessonBaseY() - rise)
    }

    private func lessonBaseY() -> CGFloat {
        isPreview ? bounds.height * 0.10 : max(bounds.height * 0.115, 96 * unit)
    }

    private func renderLesson(_ l: MinionLesson, into index: Int) {
        let width = isPreview ? bounds.width * 0.86 : min(bounds.width * 0.62, 1180 * unit)
        let serifDesc = NSFont.systemFont(ofSize: 36 * textUnit, weight: .light).fontDescriptor.withDesign(.serif)
        let serif = serifDesc.flatMap { NSFont(descriptor: $0, size: 36 * textUnit) }
            ?? NSFont(name: "Georgia", size: 34 * textUnit) ?? NSFont.systemFont(ofSize: 34 * textUnit, weight: .light)
        let para = NSMutableParagraphStyle()
        para.alignment = .center
        para.lineSpacing = 7 * textUnit
        para.lineBreakMode = .byWordWrapping
        let shadow = NSShadow()
        shadow.shadowColor = NSColor.black.withAlphaComponent(0.9)
        shadow.shadowBlurRadius = 12 * textUnit
        shadow.shadowOffset = .zero
        let body = l.cleanText
        let quoted = body.first.map { "\"\u{201C}\u{2018}'".contains($0) } ?? false
        let text = NSMutableAttributedString(string: quoted ? body : "\u{201C}" + body + "\u{201D}", attributes: [
            .font: serif, .foregroundColor: Palette.text.withAlphaComponent(0.94), .paragraphStyle: para,
            .shadow: shadow, .kern: 0.2 * textUnit,
        ])
        let attrPara = NSMutableParagraphStyle()
        attrPara.alignment = .center
        attrPara.paragraphSpacingBefore = 18 * textUnit
        let accent = l.mine ? Palette.warm : Palette.cool
        let small = NSFont.systemFont(ofSize: 17 * textUnit, weight: .light)
        text.append(NSAttributedString(string: "\n\u{2014} from \(l.title)", attributes: [
            .font: NSFontManager.shared.convert(small, toHaveTrait: .italicFontMask),
            .foregroundColor: accent.withAlphaComponent(0.9), .paragraphStyle: attrPara, .kern: 0.6 * textUnit, .shadow: shadow,
        ]))
        if !isPreview {
            var meta: [String] = []
            if let k = l.kind, !k.isEmpty { meta.append(k) }
            meta.append(l.mine ? "learned on this Mac" : "learned on \(l.node ?? "the fleet")")
            let metaPara = NSMutableParagraphStyle()
            metaPara.alignment = .center
            metaPara.paragraphSpacingBefore = 5 * textUnit
            text.append(NSAttributedString(string: "\n" + meta.joined(separator: "  \u{00B7}  ").uppercased(), attributes: [
                .font: NSFont.systemFont(ofSize: 10.5 * textUnit, weight: .medium),
                .foregroundColor: NSColor.white.withAlphaComponent(0.36), .paragraphStyle: metaPara, .kern: 1.8 * textUnit,
            ]))
        }
        guard let (img, size) = Render.text(text, width: width, scale: backing) else { return }
        let layer = lessonLayers[index]
        noActions {
            layer.contents = img
            layer.bounds = CGRect(origin: .zero, size: size)
            layer.anchorPoint = CGPoint(x: 0.5, y: 0)
            layer.position = CGPoint(x: bounds.midX, y: lessonBaseY())
            layer.opacity = 0
        }
        lessonHeights[index] = size.height
    }

    // MARK: Terminal

    private func updateTerminalTarget(_ status: MinionStatus?) {
        var lines: [String] = []
        let poller = MinionPoller.shared
        let resting = !poller.reachable || (status?.isOffline ?? true)
        var head = "nova minion"
        if let node = status?.node { head += " \u{00B7} " + node }
        if let st = status?.state, !st.isEmpty { head += " \u{00B7} " + (resting && !poller.reachable ? "resting" : st) }
        else if resting { head += " \u{00B7} resting" }
        lines.append(head)
        if !poller.reachable {
            lines.append("\u{203A} resting \u{2014} waiting for Nova to call")
        } else if let reading = status?.nowReading, !reading.isEmpty {
            for r in reading.prefix(4) {
                var s = "\u{203A} reading: \(r.title)"
                if let k = r.kind, !k.isEmpty { s += " (\(k))" }
                if let p = r.phase, !p.isEmpty, p.lowercased() != "reading" { s += " \u{00B7} \(p)\u{2026}" }
                lines.append(s)
            }
        } else if status?.isOffline == true {
            lines.append("\u{203A} hub unreachable \u{2014} holding last known sky")
        } else {
            lines.append("\u{203A} idle \u{2014} waiting for the next work")
        }
        if let m = status?.model, poller.reachable {
            var s = "  model \(m)"
            if let tk = status?.tokS { s += " \u{00B7} \(Int(tk.rounded())) tok/s" }
            lines.append(s)
        }
        let target = lines.joined(separator: "\n")
        guard target != termTarget else { return }
        let common = zip(termTarget, target).prefix { $0 == $1 }.count
        termTarget = target
        termTyped = min(termTyped, common)
    }

    private func updateTerminal(_ t: CFTimeInterval) {
        guard !isPreview else { return }
        let chars = Array(termTarget)
        if termTyped < chars.count, t - lastTypeTick > 1.0 / 32 {
            // A little human jitter: pause briefly after punctuation.
            let prev = termTyped > 0 ? chars[termTyped - 1] : " "
            if !(prev == "\u{00B7}" && t - lastTypeTick < 0.18) {
                termTyped += 1
                lastTypeTick = t
                termLastChange = t
            }
        }
        if termTyped != termRendered { renderTerminal(Array(chars.prefix(termTyped))) }
        // Cursor: solid while typing, blinking at rest.
        let typing = t - termLastChange < 0.5
        cursor.opacity = typing ? 0.9 : (fmod(t, 1.06) < 0.53 ? 0.9 : 0)
    }

    private func renderTerminal(_ visible: [Character]) {
        termRendered = visible.count
        let margin = 44 * unit
        let font = NSFont.monospacedSystemFont(ofSize: 13 * textUnit, weight: .regular)
        let para = NSMutableParagraphStyle()
        para.minimumLineHeight = termLineHeight
        para.maximumLineHeight = termLineHeight
        para.lineBreakMode = .byClipping
        let str = String(visible)
        let lines = str.split(separator: "\n", omittingEmptySubsequences: false)
        let out = NSMutableAttributedString()
        for (i, line) in lines.enumerated() {
            let color: NSColor = i == 0 ? NSColor.white.withAlphaComponent(0.42)
                : (line.hasPrefix("  ") ? Palette.terminal.withAlphaComponent(0.45) : Palette.terminal.withAlphaComponent(0.82))
            out.append(NSAttributedString(string: (i > 0 ? "\n" : "") + line,
                                          attributes: [.font: font, .foregroundColor: color, .paragraphStyle: para]))
        }
        let maxWidth = bounds.width * 0.6
        let lineCount = max(lines.count, 1)
        let size = CGSize(width: maxWidth, height: CGFloat(lineCount) * termLineHeight + 4)
        let img = Render.image(size: size, scale: backing) { _ in
            out.draw(with: CGRect(x: 0, y: 0, width: size.width, height: size.height), options: [.usesLineFragmentOrigin])
        }
        terminal.contents = img
        terminal.bounds = CGRect(origin: .zero, size: size)
        let top = bounds.height - margin
        terminal.position = CGPoint(x: margin, y: top)
        let lastLen = CGFloat(lines.last?.count ?? 0)
        let cw = monoAdvance * 0.62, ch = termLineHeight * 0.68
        cursor.bounds = CGRect(x: 0, y: 0, width: cw, height: ch)
        let lineTop = top - CGFloat(lineCount - 1) * termLineHeight
        cursor.position = CGPoint(x: margin + min(lastLen * monoAdvance, maxWidth) + cw / 2 + 2 * textUnit,
                                  y: lineTop - termLineHeight * 0.5 - 1 * textUnit)
    }

    // MARK: Status line

    private func updateStatusLine() {
        let poller = MinionPoller.shared
        let s = poller.status
        let fmt = NumberFormatter()
        fmt.numberStyle = .decimal
        func n(_ v: Int) -> String { fmt.string(from: NSNumber(value: v)) ?? "\(v)" }

        var parts: [String] = []
        var accentFirst = false
        if !poller.reachable || s?.isOffline == true {
            var rest = poller.reachable ? "Nova minion is resting \u{2014} the hub is out of reach" : "Nova minion is resting"
            if let last = poller.lastGood {
                let mins = Int(Date().timeIntervalSince(last) / 60)
                rest += mins < 1 ? " \u{00B7} last heard just now" : " \u{00B7} last heard \(Self.ago(mins)) ago"
            }
            parts.append(rest)
            if let w = s?.tonightWorks { parts.append("\(n(w)) works read tonight") }
        } else {
            if let w = s?.tonightWorks { parts.append("This Mac has read \(n(w)) works for Nova tonight"); accentFirst = true }
            if let l = s?.tonightLessons { parts.append("\(n(l)) lessons") }
            if let b = s?.tonightBeliefs, b > 0 { parts.append("\(n(b)) beliefs") }
            if let tk = s?.tokS { parts.append("\(Int(tk.rounded())) tok/s") }
            var fleet: [String] = []
            if let m = s?.fleetMinions { fleet.append("\(n(m)) minion\(m == 1 ? "" : "s")") }
            if let w = s?.fleetWorks { fleet.append("\(n(w)) works") }
            if !fleet.isEmpty { parts.append("fleet: " + fleet.joined(separator: ", ")) }
            if let a = s?.queue["A"] ?? (s?.queue.isEmpty == false ? s?.queue.values.reduce(0, +) : nil) {
                var q = "\(n(a)) still to read"
                if let eta = s?.etaDays, eta > 0 { q += " (~\(eta < 10 ? String(format: "%.1f", eta) : String(Int(eta.rounded()))) days)" }
                parts.append(q)
            }
            if parts.isEmpty { parts.append("Nova minion is listening") }
        }
        let text = parts.joined(separator: "   \u{00B7}   ")
        guard text != statusText else { return }
        statusText = text
        let size = (isPreview ? 9 : 13) * textUnit
        let font = NSFont.systemFont(ofSize: size, weight: .regular)
        let para = NSMutableParagraphStyle()
        para.alignment = .center
        let attr = NSMutableAttributedString(string: text, attributes: [
            .font: font, .foregroundColor: NSColor.white.withAlphaComponent(0.46), .paragraphStyle: para, .kern: 0.4 * textUnit,
        ])
        if accentFirst, let first = parts.first {
            attr.addAttribute(.foregroundColor, value: Palette.warm.withAlphaComponent(0.78), range: NSRange(location: 0, length: (first as NSString).length))
        }
        guard let (img, isz) = Render.text(attr, width: bounds.width - 60 * unit, scale: backing) else { return }
        statusLine.contents = img
        statusLine.bounds = CGRect(origin: .zero, size: isz)
        statusLine.position = CGPoint(x: bounds.midX, y: (isPreview ? 8 : 30) * unit)
    }

    private static func ago(_ mins: Int) -> String {
        if mins < 60 { return "\(mins) min" }
        if mins < 60 * 48 { return "\(mins / 60) h" }
        return "\(mins / 1440) days"
    }

    // MARK: Wordmark

    private func renderWordmark() {
        let font = NSFont.systemFont(ofSize: 22 * textUnit, weight: .ultraLight)
        let sub = NSFont.systemFont(ofSize: 9 * textUnit, weight: .medium)
        let para = NSMutableParagraphStyle()
        para.alignment = .right
        let s = NSMutableAttributedString(string: "NOVA", attributes: [
            .font: font, .foregroundColor: NSColor.white, .kern: 14 * textUnit, .paragraphStyle: para,
        ])
        if !isPreview {
            let subPara = NSMutableParagraphStyle()
            subPara.alignment = .right
            subPara.paragraphSpacingBefore = 2 * textUnit
            s.append(NSAttributedString(string: "\nMINION", attributes: [
                .font: sub, .foregroundColor: Palette.warm.withAlphaComponent(0.8), .kern: 5.4 * textUnit, .paragraphStyle: subPara,
            ]))
        }
        guard let (img, size) = Render.text(s, width: 400 * textUnit, scale: backing) else { return }
        wordmark.contents = img
        wordmark.bounds = CGRect(origin: .zero, size: size)
        // Kerning adds trailing space after the last glyph; nudge right to keep the margin optical.
        let margin = (isPreview ? 14 : 44) * unit
        wordmark.position = CGPoint(x: bounds.width - margin + 12 * textUnit, y: bounds.height - margin + 6 * textUnit)
    }
}
