#!/usr/bin/env swift
// Draws Keryx's app icon and writes the two .icns files the app is built with.
//
//   swift macos/scripts/make-icon.swift [--out-dir macos/Resources] [--preview DIR]
//
// The icon is the mark from docs/assets/keryx-mark.svg (three rounded bars of a voice, the
// tallest in the middle, in its orange, pink and violet) on graphite. Only AppKit and Core
// Graphics, so it runs wherever `swift` does, with nothing to install.
//
// Two files, because macOS 26 changed what an icon is and a .icns cannot say which system it
// is for (a layered .icon from Icon Composer can, but compiling one needs Xcode's actool):
//
// - AppIcon.icns, for macOS 26 and later: full-bleed square art. The system cuts it to its
//   own rounded square and adds the edge light and the shadow. Art that is already a rounded
//   square is treated as a legacy icon and set, shrunk, inside a second grey tile.
// - AppIcon-legacy.icns, for macOS 14 and 15: the classic grid, the art on an 824 px
//   continuous-corner tile in a 1024 canvas, with its own rim and shadow.
//
// macos/scripts/build-app.sh picks one by the macOS it builds on, which is the one the app
// runs on, since the app is built from the checkout.
//
// 64 px and up are the 1024 drawing scaled. 16 and 32 are drawn again by hand in whole
// pixels and flat colours: scaled down, the bars land on fractions of a pixel and blur.

import AppKit

// --- the palette ------------------------------------------------------------------------

func rgb(_ hex: UInt32, _ alpha: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat(hex >> 16 & 0xff) / 255, green: CGFloat(hex >> 8 & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
}

/// The mark's colours on a dark ground (keryx-mark.svg's dark-mode tones), left to right.
let tones: [UInt32] = [0xfb923c, 0xf472b6, 0xa78bfa]
/// A lighter tint of each, for the top of its bar: without it the violet recedes against the
/// graphite while the orange and pink stand forward.
let tints: [UInt32] = [0xfca668, 0xf68dc5, 0xb5a0fc]
let fieldTop: UInt32 = 0x34343c, fieldBottom: UInt32 = 0x1f1f26
let rim: UInt32 = 0x50505a
let glow: UInt32 = 0xdb2777
let sRGB = CGColorSpace(name: CGColorSpace.sRGB)!

func gradient(_ colors: [CGColor]) -> CGGradient {
    CGGradient(colorsSpace: sRGB, colors: colors as CFArray, locations: nil)!
}

// --- the two layouts ----------------------------------------------------------------------

let canvas: CGFloat = 1024

enum Style: CaseIterable {
    case modern, legacy

    var file: String { self == .modern ? "AppIcon.icns" : "AppIcon-legacy.icns" }

    /// Where the graphite field is drawn.
    var field: CGRect {
        self == .modern ? CGRect(x: 0, y: 0, width: canvas, height: canvas)
                        : CGRect(x: 100, y: 100, width: 824, height: 824)
    }

    /// The mark is designed against the 824 tile; full bleed, the system shrinks the whole
    /// canvas into that tile, so the mark is drawn larger by the same ratio.
    var markScale: CGFloat { self == .modern ? canvas / 824 : 1 }
}

/// The continuous-corner tile, as a superellipse (exponent 5): the shape the macOS template's
/// rounded square approximates.
func squircle(_ rect: CGRect, exponent n: CGFloat = 5) -> CGPath {
    let path = CGMutablePath()
    let steps = 1440
    for i in 0...steps {
        let t = CGFloat(i) / CGFloat(steps) * 2 * .pi
        let c = cos(t), s = sin(t)
        let point = CGPoint(x: rect.midX + rect.width / 2 * (c < 0 ? -1 : 1) * pow(abs(c), 2 / n),
                            y: rect.midY + rect.height / 2 * (s < 0 ? -1 : 1) * pow(abs(s), 2 / n))
        i == 0 ? path.move(to: point) : path.addLine(to: point)
    }
    path.closeSubpath()
    return path
}

/// The three bars as capsules, centred: 96 wide and 72 apart, the middle one 540 tall and the
/// outer two 308, on the 824 tile; times `scale`.
func bars(scale: CGFloat) -> [CGRect] {
    let width = 96 * scale, gap = 72 * scale
    return [308, 540, 308].enumerated().map { i, height in
        let centre = canvas / 2 + CGFloat(i - 1) * (width + gap)
        let h = height * scale
        return CGRect(x: centre - width / 2, y: canvas / 2 - h / 2, width: width, height: h)
    }
}

// --- the 1024 drawing ---------------------------------------------------------------------

func drawLarge(_ ctx: CGContext, _ style: Style) {
    let field = style.field
    let shape = style == .modern ? CGPath(rect: field, transform: nil) : squircle(field)

    if style == .legacy {  // the tile's own shadow; on macOS 26 the system draws it
        ctx.saveGState()
        ctx.setShadow(offset: CGSize(width: 0, height: -10), blur: 28, color: rgb(0x000000, 0.30))
        ctx.addPath(shape)
        ctx.setFillColor(rgb(fieldBottom))
        ctx.fillPath()
        ctx.restoreGState()
    }

    ctx.saveGState()
    ctx.addPath(shape)
    ctx.clip()
    ctx.drawLinearGradient(gradient([rgb(fieldTop), rgb(fieldBottom)]),
                           start: CGPoint(x: 0, y: field.maxY), end: CGPoint(x: 0, y: field.minY), options: [])
    let centre = CGPoint(x: field.midX, y: field.midY)
    ctx.drawRadialGradient(gradient([rgb(glow, 0.14), rgb(glow, 0)]), startCenter: centre, startRadius: 0,
                           endCenter: centre, endRadius: 380 * style.markScale, options: [])
    if style == .legacy {  // the rim and the light on the top edge; on macOS 26 the system's
        ctx.addPath(shape)
        ctx.setLineWidth(6)  // half of it falls outside the clip: a 3 px line
        ctx.setStrokeColor(rgb(rim, 0.40))
        ctx.strokePath()
        ctx.addPath(shape)
        ctx.replacePathWithStrokedPath()
        ctx.clip()
        ctx.drawLinearGradient(gradient([rgb(0xffffff, 0.10), rgb(0xffffff, 0)]),
                               start: CGPoint(x: 0, y: field.maxY), end: CGPoint(x: 0, y: field.maxY - 220),
                               options: [])
    }
    ctx.restoreGState()

    let scale = style.markScale
    for (i, bar) in bars(scale: scale).enumerated() {
        let capsule = CGPath(roundedRect: bar, cornerWidth: bar.width / 2, cornerHeight: bar.width / 2, transform: nil)
        ctx.saveGState()  // lifted a little off the field
        ctx.setShadow(offset: CGSize(width: 0, height: -6 * scale), blur: 16 * scale, color: rgb(0x000000, 0.35))
        ctx.addPath(capsule)
        ctx.setFillColor(rgb(tones[i]))
        ctx.fillPath()
        ctx.restoreGState()
        ctx.saveGState()
        ctx.addPath(capsule)
        ctx.clip()
        ctx.drawLinearGradient(gradient([rgb(tints[i]), rgb(tones[i])]),
                               start: CGPoint(x: 0, y: bar.maxY), end: CGPoint(x: 0, y: bar.minY), options: [])
        ctx.restoreGState()
    }
}

// --- 16 and 32, by hand -------------------------------------------------------------------

/// A small icon in whole pixels: the field and its corner radius (0 for full bleed), each
/// bar's width, the gap between bars, and the middle and outer bar heights.
struct Small {
    let field: CGRect, radius: CGFloat, bar: CGFloat, gap: CGFloat, tall: CGFloat, short: CGFloat
}

let smalls: [Style: [Int: Small]] = [
    .modern: [
        16: Small(field: CGRect(x: 0, y: 0, width: 16, height: 16), radius: 0, bar: 2, gap: 2, tall: 12, short: 6),
        32: Small(field: CGRect(x: 0, y: 0, width: 32, height: 32), radius: 0, bar: 4, gap: 3, tall: 22, short: 12),
    ],
    .legacy: [
        16: Small(field: CGRect(x: 1, y: 1, width: 14, height: 14), radius: 3.5, bar: 2, gap: 1, tall: 10, short: 6),
        32: Small(field: CGRect(x: 3, y: 3, width: 26, height: 26), radius: 6.5, bar: 4, gap: 2, tall: 18, short: 10),
    ],
]

func drawSmall(_ ctx: CGContext, _ s: Small) {
    let shape = CGPath(roundedRect: s.field, cornerWidth: s.radius, cornerHeight: s.radius, transform: nil)
    ctx.saveGState()
    ctx.addPath(shape)
    ctx.clip()
    // Lighter than the large field, so the edge holds against a dark sidebar or menu.
    ctx.drawLinearGradient(gradient([rgb(0x45454f), rgb(0x2a2a32)]),
                           start: CGPoint(x: 0, y: s.field.maxY), end: CGPoint(x: 0, y: s.field.minY), options: [])
    ctx.restoreGState()
    if s.radius > 0 {
        ctx.addPath(CGPath(roundedRect: s.field.insetBy(dx: 0.5, dy: 0.5), cornerWidth: s.radius - 0.5,
                           cornerHeight: s.radius - 0.5, transform: nil))
        ctx.setLineWidth(1)
        ctx.setStrokeColor(rgb(0x5c5c68, 0.55))
        ctx.strokePath()
    }

    let width = s.bar * 3 + s.gap * 2
    let left = s.field.midX - width / 2
    for (i, height) in [s.short, s.tall, s.short].enumerated() {
        let rect = CGRect(x: left + CGFloat(i) * (s.bar + s.gap), y: s.field.midY - height / 2,
                          width: s.bar, height: height)
        let r = min(s.bar / 2, 1.5)
        ctx.addPath(CGPath(roundedRect: rect, cornerWidth: r, cornerHeight: r, transform: nil))
        ctx.setFillColor(rgb(tones[i]))
        ctx.fillPath()
    }
}

// --- output -------------------------------------------------------------------------------

func render(_ pixels: Int, _ style: Style) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                               colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    let context = NSGraphicsContext(bitmapImageRep: rep)!
    NSGraphicsContext.current = context
    let ctx = context.cgContext
    ctx.interpolationQuality = .high
    if let small = smalls[style]?[pixels] {
        drawSmall(ctx, small)
    } else {
        ctx.scaleBy(x: CGFloat(pixels) / canvas, y: CGFloat(pixels) / canvas)
        drawLarge(ctx, style)
    }
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write("make-icon: \(message)\n".data(using: .utf8)!)
    exit(1)
}

let arguments = Array(CommandLine.arguments.dropFirst())
func option(_ name: String) -> String? {
    guard let i = arguments.firstIndex(of: name), i + 1 < arguments.count else { return nil }
    return arguments[i + 1]
}
let outDir = URL(fileURLWithPath: option("--out-dir") ?? "macos/Resources")
let fm = FileManager.default

do {
    if let preview = option("--preview") {
        let dir = URL(fileURLWithPath: preview)
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        for style in Style.allCases {
            for pixels in [1024, 512, 256, 128, 64, 32, 16] {
                try render(pixels, style).write(to: dir.appendingPathComponent("\(style)-\(pixels).png"))
            }
        }
    }

    try fm.createDirectory(at: outDir, withIntermediateDirectories: true)
    for style in Style.allCases {
        // The iconset iconutil wants: each point size at 1x and 2x.
        let iconset = fm.temporaryDirectory.appendingPathComponent(
            "Keryx-\(style)-\(ProcessInfo.processInfo.processIdentifier).iconset")
        try? fm.removeItem(at: iconset)
        try fm.createDirectory(at: iconset, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: iconset) }
        for points in [16, 32, 128, 256, 512] {
            try render(points, style).write(to: iconset.appendingPathComponent("icon_\(points)x\(points).png"))
            try render(points * 2, style).write(to: iconset.appendingPathComponent("icon_\(points)x\(points)@2x.png"))
        }
        let output = outDir.appendingPathComponent(style.file)
        let iconutil = Process()
        iconutil.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
        iconutil.arguments = ["--convert", "icns", "--output", output.path, iconset.path]
        try iconutil.run()
        iconutil.waitUntilExit()
        guard iconutil.terminationStatus == 0 else { fail("iconutil exited \(iconutil.terminationStatus)") }
        print(output.path)
    }
} catch {
    fail(error.localizedDescription)
}
