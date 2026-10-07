import AppKit
import SwiftUI
import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// Every catalog entry, drawn by the offscreen renderer the snapshots use. The render tests
/// never use the real-window capture: that needs Screen Recording, which CI does not have.
@MainActor @Suite(.serialized) struct RenderTests {
    init() {
        // A hosting view needs an application object to draw; the test process has none.
        _ = NSApplication.shared
    }

    @Test func catalogStartsWithTheMarkAndTheMenuBarIcons() {
        let ids = ScreenCatalog.entries().map(\.id)
        #expect(Array(ids.prefix(2)) == ["mark", "menubar-icons"])
        #expect(Set(ids).count == ids.count, "catalog ids must be unique: \(ids)")
    }

    @Test func blankDetectorSeesABlankView() {
        // The detector the next test relies on must be able to fail.
        let rep = Snapshot.render(AnyView(Color.clear), size: CGSize(width: 200, height: 120), dark: false)
        #expect(Self.distinctColours(rep) <= 20)
    }

    @Test func markRendersAtItsSize() {
        let entry = try! #require(ScreenCatalog.entries().first { $0.id == "mark" })
        let rep = Snapshot.render(entry.view(), size: entry.size, dark: false)
        #expect(rep.size == entry.size)
        #expect(rep.pixelsWide >= Int(entry.size.width))
        #expect(Self.distinctColours(rep) > 20)
    }

    @Test func everyEntryRendersSomething() {
        for entry in ScreenCatalog.entries() {
            let rep = Snapshot.render(entry.view(), size: entry.size, dark: false)
            #expect(Self.distinctColours(rep) > 20, "\(entry.id) rendered blank")
        }
    }

    @Test func darkDiffersFromLight() {
        let entry = try! #require(ScreenCatalog.entries().first { $0.id == "mark" })
        let light = Snapshot.render(entry.view(), size: entry.size, dark: false)
        let dark = Snapshot.render(entry.view(), size: entry.size, dark: true)
        #expect(Self.pixel(light, 2, 2) != Self.pixel(dark, 2, 2), "the window background follows the appearance")
    }

    @Test func markUsesTheSVGPaletteSwitchedOnColourScheme() {
        let entry = AnyView(KeryxMark().frame(width: 64, height: 64))
        let light = Snapshot.render(entry, size: CGSize(width: 64, height: 64), dark: false)
        let dark = Snapshot.render(entry, size: CGSize(width: 64, height: 64), dark: true)
        // The centre of each bar, in the SVG's own 64-unit box: x 20, 32, 44 at y 32.
        #expect(Self.near(Self.pixel(light, 20, 32, points: true), 0xF97316))
        #expect(Self.near(Self.pixel(light, 32, 32, points: true), 0xDB2777))
        #expect(Self.near(Self.pixel(light, 44, 32, points: true), 0x7C3AED))
        #expect(Self.near(Self.pixel(dark, 20, 32, points: true), 0xFB923C))
        #expect(Self.near(Self.pixel(dark, 32, 32, points: true), 0xF472B6))
        #expect(Self.near(Self.pixel(dark, 44, 32, points: true), 0xA78BFA))
        // Between bars is the background, and the centre bar reaches higher than the outer ones.
        #expect(!Self.near(Self.pixel(light, 26, 32, points: true), 0xF97316))
        #expect(Self.near(Self.pixel(light, 32, 15, points: true), 0xDB2777))
        #expect(!Self.near(Self.pixel(light, 20, 15, points: true), 0xF97316))
    }

    // MARK: - Helpers

    /// Distinct RGBA values over a 128 × 128 grid sampled across the image.
    static func distinctColours(_ rep: NSBitmapImageRep) -> Int {
        var seen = Set<UInt32>()
        let steps = 128
        for i in 0..<steps {
            for j in 0..<steps {
                let x = i * rep.pixelsWide / steps
                let y = j * rep.pixelsHigh / steps
                seen.insert(pixel(rep, x, y))
            }
        }
        return seen.count
    }

    /// The pixel at `(x, y)` as sRGB `0xRRGGBBAA`, in pixels, or in points with `points: true`.
    ///
    /// Read raw and converted from the rep's own colour space: `colorAt` labels the
    /// renderer's Display P3 data as Generic RGB, which would shift every colour.
    static func pixel(_ rep: NSBitmapImageRep, _ x: Int, _ y: Int, points: Bool = false) -> UInt32 {
        let scale = points ? Double(rep.pixelsWide) / rep.size.width : 1
        let px = Int(Double(x) * scale), py = Int(Double(y) * scale)
        var raw = [Int](repeating: 0, count: rep.samplesPerPixel)
        rep.getPixel(&raw, atX: px, y: py)
        let maximum = CGFloat((1 << rep.bitsPerSample) - 1)
        var components = raw.map { CGFloat($0) / maximum }
        if !rep.hasAlpha { components.append(1) }
        let native = NSColor(colorSpace: rep.colorSpace, components: &components, count: components.count)
        guard let colour = native.usingColorSpace(.sRGB) else { return 0 }
        let r = UInt32((colour.redComponent * 255).rounded())
        let g = UInt32((colour.greenComponent * 255).rounded())
        let b = UInt32((colour.blueComponent * 255).rounded())
        let a = UInt32((colour.alphaComponent * 255).rounded())
        return r << 24 | g << 16 | b << 8 | a
    }

    /// Whether an `0xRRGGBBAA` pixel is within a few steps of the opaque `0xRRGGBB` colour.
    static func near(_ rgba: UInt32, _ rgb: UInt32, tolerance: Int = 6) -> Bool {
        let channels = [(rgba >> 24, rgb >> 16), (rgba >> 16, rgb >> 8), (rgba >> 8, rgb)]
        return channels.allSatisfy { abs(Int($0.0 & 0xFF) - Int($0.1 & 0xFF)) <= tolerance }
    }
}
