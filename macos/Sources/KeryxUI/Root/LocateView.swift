import KeryxKit
import SwiftUI
import UniformTypeIdentifiers

/// What the window shows when no `keryx` could be found: a way to install Keryx, a way to
/// point the app at a checkout, and where it looked, under Details.
public struct LocateView: View {
    static let installURL = URL(string: "https://github.com/wak31415/keryx#quick-start")!

    @Environment(AppModel.self) private var model
    @Environment(\.preferences) private var preferences
    @Environment(\.openURL) private var openURL
    @State private var choosing = false
    /// Whether Details starts open (the catalog's opened state).
    private let detailsOpen: Bool

    public init() {
        detailsOpen = false
    }

    package init(detailsOpen: Bool) {
        self.detailsOpen = detailsOpen
    }

    public var body: some View {
        ContentUnavailableView {
            Label {
                Text("Keryx Isn't on This Mac")
            } icon: {
                KeryxMark()
                    .frame(width: 56, height: 56)
            }
        } description: {
            Text("Install Keryx, or choose the folder it was cloned into.")
        } actions: {
            VStack(spacing: 16) {
                HStack {
                    Button("Choose Keryx Folder…") { choosing = true }
                    Button("Install Keryx") { openURL(Self.installURL) }
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut(.defaultAction)
                }
                .fixedSize()
                if !lookedAt.isEmpty {
                    OnboardingDetails(lines: lookedAt, open: detailsOpen)
                }
            }
        }
        .fileImporter(isPresented: $choosing, allowedContentTypes: [.folder]) { result in
            guard case let .success(url) = result else { return }
            preferences.keryxFolder = url.path
            Task { await model.relocate() }
        }
        .fileDialogMessage("Choose the folder Keryx was cloned into.")
        .fileDialogConfirmationLabel("Choose")
    }

    /// Where the locator looked, one sentence each.
    private var lookedAt: [String] {
        if case let .notFound(failure) = model.location.error { return failure.lookedAt }
        return []
    }
}

/// "Details", closed: what an onboarding screen found, for the owner who wants to know why.
/// The control is centred under the buttons; only what it opens is set to a reading width.
struct OnboardingDetails: View {
    let lines: [String]
    @State private var open: Bool

    init(lines: [String], open: Bool = false) {
        self.lines = lines
        _open = State(initialValue: open)
    }

    var body: some View {
        VStack(spacing: 8) {
            Button {
                withAnimation(.snappy) { open.toggle() }
            } label: {
                Label("Show Details", systemImage: "chevron.right")
                    .labelStyle(DisclosureLabelStyle(open: open))
            }
            .buttonStyle(.borderless)
            .foregroundStyle(.secondary)
            .accessibilityValue(open ? "Expanded" : "Collapsed")
            if open {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(lines, id: \.self) { line in
                        Text(Wording.sentence(line.replacingOccurrences(of: "`", with: "")))
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .textSelection(.enabled)
                .frame(width: 440, alignment: .leading)
            }
        }
    }
}

/// A disclosure's chevron before its title, turned down while it is open.
private struct DisclosureLabelStyle: LabelStyle {
    let open: Bool

    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 4) {
            configuration.icon
                .font(.caption.weight(.semibold))
                .rotationEffect(.degrees(open ? 90 : 0))
            configuration.title
        }
    }
}
