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
