import SwiftUI

/// The Keryx mark: three rounded bars, the tallest in the middle — the voice.
///
/// The geometry is `docs/assets/keryx-mark.svg`'s, in its own 64 × 64 box: strokes 6 wide
/// with round caps at x 20 and 44 (y 24…40) and x 32 (y 16…48), scaled to fit whatever frame
/// the mark is given and centred in it. The three colours are the SVG's, switched on the
/// colour scheme; they appear on the mark and the app icon only, never elsewhere in the app.
public struct KeryxMark: View {
    public enum Mode: Sendable {
        case idle, working
        /// Reserved for the local voice channel; drawn as `.idle` for now.
        case listening, speaking
    }

    private let mode: Mode
    private let colored: Bool

    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// `colored: false` draws all three bars in the foreground style (`.primary` unless the
    /// caller sets another), for template use.
    public init(mode: Mode = .idle, colored: Bool = true) {
        self.mode = mode
        self.colored = colored
    }

    public var body: some View {
        Group {
            if mode == .working && !reduceMotion {
                TimelineView(.animation) { context in
                    canvas(Self.bars(time: context.date.timeIntervalSinceReferenceDate))
                }
            } else {
                canvas(Self.bars(time: nil))
            }
        }
        .aspectRatio(1, contentMode: .fit)
        .accessibilityElement()
        .accessibilityLabel("Keryx")
        .accessibilityAddTraits(.isImage)
    }

    private func canvas(_ bars: [Bar]) -> some View {
        let dark = colorScheme == .dark
        let colored = colored
        return Canvas { context, size in
            let scale = min(size.width, size.height) / Self.box
            let dx = (size.width - Self.box * scale) / 2
            let dy = (size.height - Self.box * scale) / 2
            let style = StrokeStyle(lineWidth: Self.strokeWidth * scale, lineCap: .round)
            for bar in bars {
                var path = Path()
                path.move(to: CGPoint(x: dx + bar.x * scale, y: dy + bar.top * scale))
                path.addLine(to: CGPoint(x: dx + bar.x * scale, y: dy + bar.bottom * scale))
                let shading: GraphicsContext.Shading =
                    colored ? .color(Color(hex: Self.hex(bar.tint, dark: dark))) : .foreground
                context.stroke(path, with: shading, style: style)
            }
        }
    }

    // MARK: - Geometry and palette (internal, for the tests)

    enum Tint: Sendable, Equatable { case a, b, c }

    struct Bar: Sendable, Equatable {
        let x: CGFloat
        let top: CGFloat
        let bottom: CGFloat
        let tint: Tint
    }

    nonisolated static let box: CGFloat = 64
    nonisolated static let strokeWidth: CGFloat = 6
    /// Seconds for one rise and fall while working: slow, a breath rather than a meter.
    nonisolated static let period: Double = 2.4

    /// The SVG's three paths, in its drawing order: a, c, then b (the centre) on top.
    /// `phase` offsets each bar's wave while working, so they do not move in lockstep.
    nonisolated private static let idle: [(x: CGFloat, half: CGFloat, tint: Tint, phase: Double)] = [
        (20, 8, .a, 0.9),
        (44, 8, .c, 1.8),
        (32, 16, .b, 0),
    ]

    /// The bars at rest (`time == nil`), or while working at `time` seconds: each bar's
    /// length follows a slow sine between 40% and 100% of its resting length, centred on
    /// y 32, so the mark never grows past its resting box.
    nonisolated static func bars(time: Double?) -> [Bar] {
        idle.map { bar in
            var half = bar.half
            if let time {
                let wave = 0.5 + 0.5 * sin(2 * .pi * time / period + bar.phase)
                half *= CGFloat(0.4 + 0.6 * wave)
            }
            return Bar(x: bar.x, top: box / 2 - half, bottom: box / 2 + half, tint: bar.tint)
        }
    }

    /// `0xRRGGBB`, exactly the SVG's light and dark strokes.
    nonisolated static func hex(_ tint: Tint, dark: Bool) -> UInt32 {
        switch (tint, dark) {
        case (.a, false): 0xF97316
        case (.b, false): 0xDB2777
        case (.c, false): 0x7C3AED
        case (.a, true): 0xFB923C
        case (.b, true): 0xF472B6
        case (.c, true): 0xA78BFA
        }
    }
}

extension Color {
    /// An opaque sRGB colour from `0xRRGGBB`.
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255)
    }
}
