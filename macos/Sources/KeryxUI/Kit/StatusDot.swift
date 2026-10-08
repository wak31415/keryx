import SwiftUI

/// An 8 pt status dot. It carries no words of its own: always put it beside text that says
/// the same thing, so colour is never the only signal (it is hidden from VoiceOver).
public struct StatusDot: View {
    public enum Tone: Sendable, CaseIterable {
        case ok, warning, error, inactive
    }

    private let tone: Tone

    public init(_ tone: Tone) {
        self.tone = tone
    }

    public var body: some View {
        Circle()
            .fill(style)
            .frame(width: 8, height: 8)
            .accessibilityHidden(true)
    }

    private var style: AnyShapeStyle {
        switch tone {
        case .ok: AnyShapeStyle(.green)
        case .warning: AnyShapeStyle(.orange)
        case .error: AnyShapeStyle(.red)
        case .inactive: AnyShapeStyle(.secondary)
        }
    }
}

/// The glyph beside one of doctor's checks, wherever one is shown (the Overview, a Setup
/// Assistant step): a green check when it passed, an orange ring when something is still to
/// do, a red mark when it failed. The setup wizard's list draws a done or failed step the
/// same way. Always beside words that say the same; `label` is what VoiceOver says.
public struct CheckGlyph: Sendable {
    public let symbol: String
    public let colour: Color
    public let label: String

    public static let ok = CheckGlyph(symbol: "checkmark.circle.fill", colour: .green, label: "Passed")
    public static let missing = CheckGlyph(symbol: "exclamationmark.circle", colour: .orange, label: "Needs attention")
    public static let failed = CheckGlyph(symbol: "xmark.octagon.fill", colour: .red, label: "Failed")

    /// Something optional left off: grey, as the setup wizard's optional steps are.
    public static let off = CheckGlyph(symbol: "circle.dotted", colour: .secondary, label: "Off")

    /// For a check: `of(state)`, except one missing only because something optional is off
    /// ("off (optional)"), which needs no attention.
    public static func of(_ state: String, detail: String) -> CheckGlyph {
        if state == "missing", detail.localizedCaseInsensitiveContains("(optional)") { return off }
        return of(state)
    }

    /// For doctor's `state`: `ok`, `missing`, anything else failed.
    public static func of(_ state: String) -> CheckGlyph {
        switch state {
        case "ok": ok
        case "missing": missing
        default: failed
        }
    }

    public var image: some View {
        Image(systemName: symbol)
            .foregroundStyle(colour)
            .accessibilityLabel(label)
    }
}
