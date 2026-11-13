// winrec — record ONE window (ScreenCaptureKit, desktop-independent: nothing that overlaps it is captured).
//   winrec list                         -> windowID<TAB>app<TAB>title<TAB>WxH (on-screen windows)
//   winrec <windowID> <out.mov> [fps]   -> records until SIGINT/SIGTERM; prints START <epoch> at the first frame
// H.264 via AVAssetWriter at the window's pixel size (Retina 2x), plus the game's OWN audio (AAC 48 kHz stereo) from a
// second stream whose filter includes only the window's application (no other app, no system alerts, not us).
import Foundation
import AppKit
import ScreenCaptureKit
import AVFoundation
import CoreMedia

final class Rec: NSObject, SCStreamOutput, SCStreamDelegate {
    let writer: AVAssetWriter
    let input: AVAssetWriterInput
    let audio: AVAssetWriterInput
    var audioSamples = 0
    var audioSeen = 0
    var started = false
    var lastPTS = CMTime.zero
    init(url: URL, w: Int, h: Int, fps: Int) throws {
        writer = try AVAssetWriter(outputURL: url, fileType: .mov)
        input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: w, AVVideoHeightKey: h,
            AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 24_000_000, AVVideoExpectedSourceFrameRateKey: fps,
                                              AVVideoMaxKeyFrameIntervalKey: fps * 2]])
        input.expectsMediaDataInRealTime = true
        writer.add(input)
        audio = AVAssetWriterInput(mediaType: .audio, outputSettings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48000, AVNumberOfChannelsKey: 2, AVEncoderBitRateKey: 192_000])
        audio.expectsMediaDataInRealTime = true
        writer.add(audio)
    }
    func stream(_ s: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        if type == .audio {
            audioSeen += 1
            guard started, sb.isValid else { return }
            if audio.isReadyForMoreMediaData { audio.append(sb); audioSamples += 1 }
            return
        }
        guard type == .screen, sb.isValid,
              let att = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
              let raw = att.first?[.status] as? Int, let st = SCFrameStatus(rawValue: raw), st == .complete else { return }
        let pts = CMSampleBufferGetPresentationTimeStamp(sb)
        if !started {
            writer.startWriting(); writer.startSession(atSourceTime: pts); started = true
            print("START \(Date().timeIntervalSince1970)"); fflush(stdout)
        }
        if input.isReadyForMoreMediaData { input.append(sb); lastPTS = pts }
    }
    var restart: (() -> Void)?
    func stream(_ s: SCStream, didStopWithError e: Error) {
        // -3805 "application connection interrupted" happens when the app opens a sheet/dialog: start a new stream on
        // the same window and keep writing to the same file (a short gap, nothing else captured).
        FileHandle.standardError.write("stream stopped: \(e) — restarting\n".data(using: .utf8)!)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.restart?() }
    }
}

let args = CommandLine.arguments
_ = NSApplication.shared
_ = CGMainDisplayID()
let sema = DispatchSemaphore(value: 0)
var content: SCShareableContent!
SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: false) { c, e in
    if let e = e { FileHandle.standardError.write("no shareable content: \(e)\n".data(using: .utf8)!); exit(2) }
    content = c; sema.signal()
}
sema.wait()
if args.count < 3 {
    for w in content.windows where w.frame.width > 200 {
        print("\(w.windowID)\t\(w.owningApplication?.applicationName ?? "?")\t\(w.title ?? "")\t\(Int(w.frame.width))x\(Int(w.frame.height))\tonscreen=\(w.isOnScreen)")
    }
    exit(0)
}
guard let wid = UInt32(args[1]), let win = content.windows.first(where: { $0.windowID == wid }) else {
    FileHandle.standardError.write("window not found\n".data(using: .utf8)!); exit(2)
}
let fps = args.count > 3 ? Int(args[3]) ?? 30 : 30
let scale = 2
let w = (Int(win.frame.width) * scale) & ~1, h = (Int(win.frame.height) * scale) & ~1
let cfg = SCStreamConfiguration()
cfg.width = w; cfg.height = h
cfg.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(fps))
cfg.pixelFormat = kCVPixelFormatType_32BGRA
cfg.showsCursor = false
cfg.queueDepth = 6
// 2026-11-12: the game's audio comes from THIS (window) stream. On macOS 27 a second SCStream in the same process got no
// audio at all (0 buffers for GTNW 3.27.1 and for a whole-display mix), while the first audio-capturing stream did.
// A desktop-independent window filter carries only the owning app's audio.
cfg.capturesAudio = true; cfg.excludesCurrentProcessAudio = true; cfg.sampleRate = 48000; cfg.channelCount = 2
let filter = SCContentFilter(desktopIndependentWindow: win)
let url = URL(fileURLWithPath: args[2])
try? FileManager.default.removeItem(at: url)
let rec = try Rec(url: url, w: w, h: h, fps: fps)
let q = DispatchQueue(label: "rec")
var stream = SCStream(filter: filter, configuration: cfg, delegate: rec)
try stream.addStreamOutput(rec, type: .screen, sampleHandlerQueue: q)
try stream.addStreamOutput(rec, type: .audio, sampleHandlerQueue: q)
stream.startCapture { e in if let e = e { FileHandle.standardError.write("start failed: \(e)\n".data(using: .utf8)!); exit(2) } }
FileHandle.standardError.write("audio from \(win.owningApplication?.applicationName ?? "?") (\(win.owningApplication?.bundleIdentifier ?? "?")) only, via the window stream\n".data(using: .utf8)!)
var restarts = 0
var failures = 0
rec.restart = {
    restarts += 1
    if restarts > 200 { FileHandle.standardError.write("too many restarts\n".data(using: .utf8)!); exit(3) }
    stream = SCStream(filter: filter, configuration: cfg, delegate: rec)
    try? stream.addStreamOutput(rec, type: .screen, sampleHandlerQueue: q)
    try? stream.addStreamOutput(rec, type: .audio, sampleHandlerQueue: q)
    stream.startCapture { e in
        if let e = e {
            FileHandle.standardError.write("restart failed: \(e)\n".data(using: .utf8)!)
            failures += 1
            // the window is gone (app quit or crashed): finalise what we have instead of retrying forever
            if failures >= 5 { DispatchQueue.main.async { FileHandle.standardError.write("WINDOW GONE\n".data(using: .utf8)!); finish() } }
            else { DispatchQueue.main.asyncAfter(deadline: .now() + 2) { rec.restart?() } }
        } else { failures = 0 }
    }
}

func finish() {
    stream.stopCapture { _ in
      q.async {
        guard rec.started else { FileHandle.standardError.write("no frames captured\n".data(using: .utf8)!); exit(4) }
        rec.input.markAsFinished(); rec.audio.markAsFinished()
        FileHandle.standardError.write("audio buffers: \(rec.audioSamples) (seen \(rec.audioSeen))\n".data(using: .utf8)!)
        rec.writer.endSession(atSourceTime: rec.lastPTS)
        rec.writer.finishWriting { print("STOP \(Date().timeIntervalSince1970)"); exit(0) }
      }
    }
}
signal(SIGINT, SIG_IGN); signal(SIGTERM, SIG_IGN)
let s1 = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main); s1.setEventHandler { finish() }; s1.resume()
let s2 = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main); s2.setEventHandler { finish() }; s2.resume()
if args.count > 4, let secs = Double(args[4]) { DispatchQueue.main.asyncAfter(deadline: .now() + secs) { finish() } }
dispatchMain()
