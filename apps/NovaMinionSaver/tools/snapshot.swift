//
//  snapshot.swift — offscreen harness for the Nova Minion screensaver.
//
//  Loads the built .saver bundle via Bundle(path:).principalClass (exactly like
//  legacyScreenSaver does), hosts the view in an offscreen NSWindow, drives
//  animateOneFrame at 30 fps against the live endpoint, and writes PNG frames.
//
//  Build:  swiftc -O tools/snapshot.swift -o <out>/snapshot -framework ScreenSaver
//  Usage:  snapshot --saver NovaMinion.saver --out DIR [--size 1920x1080] [--preview]
//                   [--at 2,8,20] [--prefix normal] [--endpoint URL] [--duration S]
//

import AppKit
import QuartzCore
import ScreenSaver

var args = CommandLine.arguments.dropFirst().makeIterator()
var saverPath = ""
var outDir = FileManager.default.currentDirectoryPath
var size = CGSize(width: 1920, height: 1080)
var preview = false
var times: [Double] = [2, 8, 20]
var prefix = "frame"
var endpoint: String?
var duration: Double?
var renderScale: CGFloat = 1

while let a = args.next() {
    switch a {
    case "--saver": saverPath = args.next() ?? ""
    case "--out": outDir = args.next() ?? outDir
    case "--size":
        let p = (args.next() ?? "").split(separator: "x").compactMap { Double($0) }
        if p.count == 2 { size = CGSize(width: p[0], height: p[1]) }
    case "--preview": preview = true
    case "--at": times = (args.next() ?? "").split(separator: ",").compactMap { Double($0) }
    case "--prefix": prefix = args.next() ?? prefix
    case "--endpoint": endpoint = args.next()
    case "--duration": duration = Double(args.next() ?? "")
    case "--scale": renderScale = CGFloat(Double(args.next() ?? "1") ?? 1)
    default: FileHandle.standardError.write("unknown arg \(a)\n".data(using: .utf8)!); exit(2)
    }
}

guard let bundle = Bundle(path: saverPath), bundle.load(), let cls = bundle.principalClass as? ScreenSaverView.Type else {
    FileHandle.standardError.write("could not load principal class from \(saverPath)\n".data(using: .utf8)!)
    exit(1)
}
print("loaded \(NSStringFromClass(cls)) from \(bundle.bundlePath) [\(bundle.bundleIdentifier ?? "?")]")

// Point the saver at a specific endpoint for this run (restored at exit).
let defaults = ScreenSaverDefaults(forModuleWithName: bundle.bundleIdentifier ?? "com.jordankoch.NovaMinion")
let previousEndpoint = defaults?.string(forKey: "endpoint")
if let e = endpoint { defaults?.set(e, forKey: "endpoint"); defaults?.synchronize() }

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)

let window = NSWindow(contentRect: CGRect(origin: CGPoint(x: -10_000, y: -10_000), size: size),
                      styleMask: .borderless, backing: .buffered, defer: false)
guard let view = cls.init(frame: CGRect(origin: .zero, size: size), isPreview: preview) else { exit(1) }
window.contentView = view
// Frames are driven below (not via startAnimation) so CPU numbers reflect exactly 30 fps.

try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)

func snapshot(_ name: String) {
    guard let layer = view.layer else { return }
    let w = Int(size.width * renderScale), h = Int(size.height * renderScale)
    guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                              space: CGColorSpace(name: CGColorSpace.sRGB)!,
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return }
    ctx.scaleBy(x: renderScale, y: renderScale)
    layer.render(in: ctx)
    guard let img = ctx.makeImage() else { return }
    let rep = NSBitmapImageRep(cgImage: img)
    let path = (outDir as NSString).appendingPathComponent("\(name).png")
    try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
    print("wrote \(path)")
}

let start = Date()
var pending = times.sorted()
let end = duration ?? ((pending.last ?? 0) + 0.1)
var frames = 0
let cpuStart = clock()
while true {
    let elapsed = Date().timeIntervalSince(start)
    if elapsed >= end { break }
    view.animateOneFrame()
    frames += 1
    if let next = pending.first, elapsed >= next {
        pending.removeFirst()
        snapshot(String(format: "%@-%02ds", prefix, Int(next)))
    }
    // Pace at 30 fps while servicing the run loop (network callbacks hop to main).
    let target = start.addingTimeInterval(Double(frames) / 30.0)
    RunLoop.main.run(until: max(target, Date()))
}
let cpu = Double(clock() - cpuStart) / Double(CLOCKS_PER_SEC)
print(String(format: "frames=%d wall=%.1fs fps=%.1f cpu=%.2fs (%.1f%% of one core, incl. snapshot renders)",
             frames, end, Double(frames) / end, cpu, cpu / end * 100))

if endpoint != nil {
    if let p = previousEndpoint, p != endpoint { defaults?.set(p, forKey: "endpoint") } else { defaults?.removeObject(forKey: "endpoint") }
    defaults?.synchronize()
}
