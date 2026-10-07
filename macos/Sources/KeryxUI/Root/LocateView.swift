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

    public init() {}

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
                    OnboardingDetails(lines: lookedAt)
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
struct OnboardingDetails: View {
    let lines: [String]
    @State private var open = false

    var body: some View {
        DisclosureGroup("Details", isExpanded: $open) {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(lines, id: \.self) { line in
                    Text(Wording.sentence(line.replacingOccurrences(of: "`", with: "")))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, 4)
        }
        .frame(width: 440)
    }
}
