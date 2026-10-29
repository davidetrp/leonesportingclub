// Riconoscimento del testo con Vision di macOS: stampa le righe lette nell'immagine passata come argomento.
// Il server lo compila da solo al primo uso (swiftc), se gli strumenti per sviluppatori di Apple sono installati.
import AppKit
import Foundation
import Vision

guard CommandLine.arguments.count > 1,
      let image = NSImage(contentsOfFile: CommandLine.arguments[1]),
      let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { exit(1) }
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
request.recognitionLanguages = ["it-IT", "en-US"]
do { try VNImageRequestHandler(cgImage: cg, options: [:]).perform([request]) } catch { exit(2) }
let lines = (request.results ?? [])
    .sorted { ($0.boundingBox.maxY, -$0.boundingBox.minX) > ($1.boundingBox.maxY, -$1.boundingBox.minX) }
    .compactMap { $0.topCandidates(1).first?.string }
print(lines.joined(separator: "\n"))
