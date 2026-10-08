import KeryxKit
import SwiftUI

/// Move Your Files, while Jarvis's files are still where Jarvis kept them (`MigrationReader`,
/// shared with the main window). Nothing else is written before it. Once a step has come
/// there is no Cancel, and the window cannot close: the CLI finishes the move whatever it is
/// told. Done, the frame moves on by itself.
package struct MoveFilesFrame: View {
    let close: () -> Void

    @Environment(AppModel.self) private var model
    @Environment(SetupWizard.self) private var wizard
    @State private var reader: MigrationReader

    /// The catalog's: the plan already read.
    package init(close: @escaping () -> Void, plan: MigrationPlan? = nil) {
        self.close = close
        _reader = State(initialValue: MigrationReader(plan: plan))
    }

    package var body: some View {
        let run = model.migrationRun
        WizardPage(symbol: .system("shippingbox"), title: WizardWording.moveTitle,
                   sentence: WizardWording.moveSentence, position: wizard.position(WizardFacts(model))) {
            MigrationSections(reader: reader)
        } trailing: {
            Button("Quit Setup") { close() }
                .keyboardShortcut(.cancelAction)
                .disabled(run.isBusy)
            if model.canCancelMigration {
                Button("Cancel") { model.cancelMigration() }
            }
            MigrationPrimary(reader: reader, check: { Task { await read() } },
                             proceed: { Task { await wizard.moveOn(model) } })
        }
        .task { if reader.plan == nil { await read() } }
        .onChange(of: run.result) { _, done in
            guard let done, !done.unitsPending else { return }
            Task { await wizard.moveOn(model) }
        }
    }

    private func read() async {
        // Nothing left to move after all: on to what is.
        await reader.read(model) { await wizard.moveOn(model) }
    }
}
