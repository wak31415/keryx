import KeryxKit
import SwiftUI

/// What the window shows while Keryx refuses to run until its files from Jarvis have moved:
/// the same move as the setup wizard's (`MigrationSections`, `migrate --yes --json`), here in
/// the window, with the CLI's own sentence under Details. Once the files have moved,
/// everything is read again and the window shows Keryx.
public struct MigrationView: View {
    @Environment(AppModel.self) private var model
    @State private var reader: MigrationReader

    public init() {
        _reader = State(initialValue: MigrationReader())
    }

    /// The catalog's: the plan already read.
    package init(plan: MigrationPlan) {
        _reader = State(initialValue: MigrationReader(plan: plan))
    }

    public var body: some View {
        VStack(spacing: 16) {
            VStack(spacing: 8) {
                KeryxMark()
                    .frame(width: 56, height: 56)
                    .accessibilityHidden(true)
                Text("Keryx Needs to Move Its Files")
                    .font(.title2.weight(.semibold))
                Text(WizardWording.moveSentence)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
            MigrationSections(reader: reader)
            HStack(spacing: 10) {
                Spacer()
                if model.canCancelMigration {
                    Button("Cancel") { model.cancelMigration() }
                }
                MigrationPrimary(reader: reader, check: { Task { await reader.read(model) } })
            }
            .controlSize(.large)
            if let sentence {
                OnboardingDetails(lines: [sentence])
            }
        }
        .frame(maxWidth: 540)
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .task { if reader.plan == nil { await reader.read(model) } }
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
}
