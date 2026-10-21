import Foundation
import Vision
import AppKit
// nova_vision_ocr.swift — Apple Vision (VNRecognizeTextRequest, accurate) OCR for nova_screenplay_ingest.py.
// Far cleaner than tesseract on 1-bit 144-dpi screenplay scans (no margin junk). Compiled on demand.
// usage: nova_vision_ocr <image>...  -> prints recognized text per image, pages separated by blank lines
for path in CommandLine.arguments.dropFirst() {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = true
    req.recognitionLanguages = ["en-US"]
    try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
    let obs = (req.results ?? []).sorted { $0.boundingBox.minY > $1.boundingBox.minY }
    var lines: [String] = []
    var lastY: CGFloat? = nil
    for o in obs {
        guard let s = o.topCandidates(1).first?.string else { continue }
        if let y = lastY, y - o.boundingBox.maxY > 0.02 { lines.append("") }  // vertical gap -> paragraph
        lines.append(s); lastY = o.boundingBox.minY
    }
    print(lines.joined(separator: "\n")); print("\n")
}
