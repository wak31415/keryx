import AppKit
import KeryxKit
import SwiftUI

/// What a person might pass on but should not have to read, folded away: a tool's output, a
/// path, a log, a command for the terminal. Never the primary copy — the row above it says
/// what happened in a sentence; this holds the raw text under "Show Details", selectable and
/// with Copy.
///
/// `heading` names what it is when that is not obvious ("For the Terminal" for a doctor's
/// hint, which is a command to run elsewhere).
struct DetailsDisclosure: View {
    let text: String
    var heading: String?
    /// What to reveal in the Finder beside it (a log Keryx wrote), when there is one.
    var log: URL?

    @State private var open = false

    init(_ text: String, heading: String? = nil, log: URL? = nil) {
        self.text = text
        self.heading = heading
        self.log = log
    }

    var body: some View {
        DisclosureGroup(isExpanded: $open) {
            VStack(alignment: .leading, spacing: 6) {
                if let heading {
                    Text(heading)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
                Text(text)
                    .font(.callout.monospaced())
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                HStack(spacing: 8) {
                    Button("Copy") { Self.copy(text) }
                    if let log {
                        Button("Show Log") { RunLog.show(log) }
                            .help("Shows the log in the Finder")
                    }
                }
                .controlSize(.small)
            }
            .padding(.top, 2)
        } label: {
            Text("Show Details")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    static func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}

/// An error in a sentence, with what Keryx printed under Show Details: how every screen says
/// a failure inline. Build it from the screen's own sentence for its operation
/// (`KeryxError.said`), or from the error alone.
struct ErrorText: View {
    let failure: StreamFailure

    init(_ failure: StreamFailure) {
        self.failure = failure
    }

    init(error: KeryxError) {
        failure = StreamFailure(error)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label {
                Text(failure.message)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
            }
            if let detail = failure.detail {
                DetailsDisclosure(detail)
            }
        }
    }
}

extension View {
    /// An alert for `error`: its title and sentence, OK, and — when Keryx printed more than
    /// the sentence says — Copy Details, since an alert has no room for the raw text.
    func errorAlert(_ error: Binding<KeryxError?>) -> some View {
        alert(error.wrappedValue?.title ?? "", isPresented: Binding(
            get: { error.wrappedValue != nil }, set: { if !$0 { error.wrappedValue = nil } }
        ), presenting: error.wrappedValue) { shown in
            Button("OK") {}
                .keyboardShortcut(.defaultAction)
            if let details = shown.details {
                Button("Copy Details") { DetailsDisclosure.copy(details) }
            }
        } message: { shown in
            Text(shown.message)
        }
    }
}

/// How bad a problem a sentence says is: its icon's colour (the sentence stays secondary).
enum ProblemTone {
    case error, warning

    var symbol: String {
        switch self {
        case .error: "exclamationmark.circle.fill"
        case .warning: "exclamationmark.triangle.fill"
        }
    }

    var colour: Color {
        switch self {
        case .error: .red
        case .warning: .orange
        }
    }
}

extension View {
    /// A sentence about something wrong, legible in either appearance: the words in the
    /// secondary colour, the colour only in the icon before them.
    func problemStyle(_ tone: ProblemTone = .error) -> some View {
        Label {
            foregroundStyle(.secondary)
        } icon: {
            Image(systemName: tone.symbol)
                .foregroundStyle(tone.colour)
                .accessibilityLabel(tone == .error ? "Problem" : "Warning")
        }
    }
}
