import KeryxKit
import SwiftUI

/// What the window shows while Keryx refuses to run until `keryx migrate` has moved an older
/// install's files: the CLI's own sentence, and the command, run in Terminal.
public struct MigrationView: View {
    @Environment(AppModel.self) private var model
    @State private var failure: KeryxError?
    @State private var checking = false

    public init() {}

    public var body: some View {
        ContentUnavailableView {
            Label {
                Text("Keryx Needs to Move Its Files")
            } icon: {
                KeryxMark()
                    .frame(width: 56, height: 56)
            }
        } description: {
            Text((try? AttributedString(markdown: sentence)) ?? AttributedString(sentence))
                .textSelection(.enabled)
        } actions: {
            HStack {
                Button("Check Again") {
                    checking = true
                    Task {
                        await model.refresh(.all)
                        checking = false
                    }
                }
                .disabled(checking)
                Button("Open `keryx migrate` in Terminal") { openMigrate() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(model.location.value == nil)
            }
            .fixedSize()
        }
        .alert(failure?.title ?? "", isPresented: Binding(
            get: { failure != nil }, set: { if !$0 { failure = nil } }
        )) {
            Button("OK") {}
        } message: {
            Text(failure?.message ?? "")
        }
    }

    /// The CLI's sentence: the refusal it printed, else what the status said about storage.
    private var sentence: String {
        let raw: String? = if case let .migrationRequired(text) = model.status.error {
            text
        } else {
            model.status.value?.storage.error
        }
        guard let raw, !raw.isEmpty else {
            return "Keryx found files from an older install. Run `keryx migrate` to move them."
        }
        return Wording.capitalised(raw).hasSuffix(".") ? Wording.capitalised(raw) : Wording.capitalised(raw) + "."
    }

    private func openMigrate() {
        guard let location = model.location.value else { return }
        do {
            try TerminalLauncher.open(location, arguments: ["migrate"])
        } catch {
            failure = (error as? KeryxError) ?? .launch(error.localizedDescription)
        }
    }
}
