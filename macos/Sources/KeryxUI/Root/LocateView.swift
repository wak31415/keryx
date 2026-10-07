import KeryxKit
import SwiftUI
import UniformTypeIdentifiers

/// What the window shows when no `keryx` could be found: where the app looked, a way to
/// point it at a checkout, and a way to install Keryx.
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
            Text(lookedAt.joined(separator: "\n"))
                .textSelection(.enabled)
        } actions: {
            HStack {
                Button("Install Keryx") { openURL(Self.installURL) }
                Button("Choose Keryx Folder…") { choosing = true }
                    .keyboardShortcut(.defaultAction)
            }
            .fixedSize()
        }
        .fileImporter(isPresented: $choosing, allowedContentTypes: [.folder]) { result in
            guard case let .success(url) = result else { return }
            preferences.keryxFolder = url.path
            Task { await model.relocate() }
        }
        .fileDialogMessage("Choose the folder Keryx was cloned into.")
        .fileDialogConfirmationLabel("Choose")
    }

    private var lookedAt: [String] {
        if case let .notFound(failure) = model.location.error, !failure.lookedAt.isEmpty {
            return failure.lookedAt
        }
        return ["Install Keryx, or choose the folder it was cloned into."]
    }
}
