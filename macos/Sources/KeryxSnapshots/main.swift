import AppKit

// KeryxSnapshots <out-dir> [--offscreen] [--only PREFIX]
//
// Writes <id>-light.png and <id>-dark.png for every ScreenCatalog entry, and an index.html
// that lays them all out for a person to look at. Real windows photographed by window id
// when Screen Recording is granted; otherwise (or with --offscreen) the offscreen renderer.

NSApplication.shared.setActivationPolicy(.accessory)

var arguments = Array(CommandLine.arguments.dropFirst())
let offscreen = arguments.contains("--offscreen")
arguments.removeAll { $0 == "--offscreen" }
// --only PREFIX: just the entries whose id starts with it (`wizard-`), to look at one screen.
var only: String?
if let index = arguments.firstIndex(of: "--only"), index + 1 < arguments.count {
    only = arguments[index + 1]
    arguments.removeSubrange(index...(index + 1))
}
guard arguments.count == 1, !arguments[0].hasPrefix("-") else {
    FileHandle.standardError.write(Data("usage: KeryxSnapshots <out-dir> [--offscreen] [--only PREFIX]\n".utf8))
    exit(2)
}

do {
    try Renderer(outputDirectory: URL(fileURLWithPath: arguments[0]), offscreen: offscreen, only: only).run()
} catch {
    FileHandle.standardError.write(Data("KeryxSnapshots: \(error)\n".utf8))
    exit(1)
}
