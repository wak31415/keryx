import AppKit
import CoreGraphics
import KeryxPreview
import KeryxUI

/// Writes every catalog entry in light and dark, then the index page.
@MainActor struct Renderer {
    let outputDirectory: URL
    /// Offscreen even when real windows could be photographed.
    let offscreen: Bool
    /// Only the entries whose id starts with this; every entry when `nil`.
    var only: String?

    func run() throws {
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        let realWindows = !offscreen && CGPreflightScreenCaptureAccess()
        print(realWindows ? "Photographing real windows." : "Rendering offscreen.")
        NSApplication.shared.finishLaunching()

        let entries = ScreenCatalog.entries().filter { only.map($0.id.hasPrefix) ?? true }
        for entry in entries {
            for dark in [false, true] {
                let file = outputDirectory.appendingPathComponent(Self.fileName(entry.id, dark: dark))
                if realWindows {
                    try Snapshot.capture(entry.view(), size: entry.size, dark: dark, to: file, title: entry.id)
                } else {
                    let rep = Snapshot.render(entry.view(), size: entry.size, dark: dark)
                    guard let png = Snapshot.png(rep) else { throw RenderFailure.encode(entry.id) }
                    try png.write(to: file)
                }
                print(file.path)
            }
        }
        let index = outputDirectory.appendingPathComponent("index.html")
        try Self.index(entries.map(\.id)).write(to: index, atomically: true, encoding: .utf8)
        print(index.path)
    }

    static func fileName(_ id: String, dark: Bool) -> String {
        "\(id)-\(dark ? "dark" : "light").png"
    }

    /// A page with every entry's light and dark images side by side, for a person.
    static func index(_ ids: [String]) -> String {
        let rows = ids.map { id in
            """
              <section>
                <h2>\(id)</h2>
                <div class="pair">
                  <figure><img src="\(fileName(id, dark: false))" alt="\(id), light"><figcaption>Light</figcaption></figure>
                  <figure class="dark"><img src="\(fileName(id, dark: true))" alt="\(id), dark"><figcaption>Dark</figcaption></figure>
                </div>
              </section>
            """
        }.joined(separator: "\n")
        return """
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <title>Keryx for Mac snapshots</title>
          <style>
            body { font: 13px -apple-system, system-ui, sans-serif; margin: 24px; background: #f5f5f7; color: #1d1d1f; }
            h2 { font-size: 15px; margin: 28px 0 8px; }
            .pair { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start; }
            figure { margin: 0; padding: 12px; background: #fff; border-radius: 10px; }
            figure.dark { background: #1d1d1f; color: #f5f5f7; }
            img { display: block; max-width: 100%; height: auto; zoom: 0.5; }
            figcaption { margin-top: 6px; opacity: 0.7; }
          </style>
        </head>
        <body>
          <h1>Keryx for Mac snapshots</h1>
        \(rows)
        </body>
        </html>

        """
    }
}

enum RenderFailure: Error, CustomStringConvertible {
    case encode(String)

    var description: String {
        switch self {
        case let .encode(id): "could not encode \(id) as PNG"
        }
    }
}
