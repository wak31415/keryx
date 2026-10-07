import KeryxKit
import SwiftUI

/// What the window shows while Keryx refuses to run until `keryx migrate` has moved an older
/// install's files: what that means in a line, the command run in Terminal, and the CLI's
/// own sentence under Details.
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
            Text("Keryx found files from an older install, and moves them before it runs again.")
        } actions: {
            VStack(spacing: 16) {
                HStack {
                    Button("Check Again") {
                        checking = true
                        Task {
                            await model.refresh(.all)
                            checking = false
                        }
                    }
                    .disabled(checking)
                    Button("Migrate in Terminal") { openMigrate() }
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut(.defaultAction)
                        .disabled(model.location.value == nil)
                        .help("Runs keryx migrate in Terminal")
                }
                .fixedSize()
                if let sentence {
                    OnboardingDetails(lines: [sentence])
                }
            }
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
    private var sentence: String? {
        let raw: String? = if case let .migrationRequired(text) = model.status.error {
            text
        } else {
            model.status.value?.storage.error
        }
        guard let raw, !raw.isEmpty else { return nil }
        return raw
    }

    private func openMigrate() {
        guard let location = model.location.value else { return }
        do {
            try TerminalLauncher.open(location, arguments: ["migrate"])
        } catch {
            failure = (error as? KeryxError) ?? .terminal(error.localizedDescription)
        }
    }
}
