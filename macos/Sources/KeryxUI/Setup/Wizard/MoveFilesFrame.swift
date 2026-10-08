import KeryxKit
import SwiftUI

/// Move Your Files, while Jarvis's files are still where Jarvis kept them: what moving them
/// does (`migrate --json`), then the move itself (`migrate --yes --json`). Nothing else is
/// written before it. Once a step has come there is no Cancel, and the window cannot close:
/// the CLI finishes the move whatever it is told. Done, the frame moves on by itself.
package struct MoveFilesFrame: View {
    let close: () -> Void

    @Environment(AppModel.self) private var model
    @Environment(SetupWizard.self) private var wizard
    @State private var plan: MigrationPlan?
    @State private var readError: KeryxError?
    @State private var reading = false

    /// The catalog's: the plan already read.
    package init(close: @escaping () -> Void, plan: MigrationPlan? = nil) {
        self.close = close
        _plan = State(initialValue: plan)
    }

    package var body: some View {
        let run = model.migrationRun
        WizardPage(symbol: .system("shippingbox"), title: WizardWording.moveTitle,
                   sentence: WizardWording.moveSentence, position: wizard.position(WizardFacts(model))) {
            VStack(spacing: 12) {
                if let plan {
                    summary(plan)
                    if !plan.conflicts.isEmpty { conflicts(plan) }
                } else if let readError {
                    WizardSection(title: "What Moves") { ErrorText(readError.said("Keryx couldn't say what moves.")) }
                } else {
                    WizardSection(title: "What Moves") {
                        StatusLine(tone: .working, text: "Looking at what Jarvis left…")
                    }
                }
                progress(run)
            }
        } trailing: {
            Button("Quit Setup") { close() }
                .keyboardShortcut(.cancelAction)
                .disabled(run.isBusy)
            if model.canCancelMigration {
                Button("Cancel") { model.cancelMigration() }
            }
            primary(run)
        }
        .task { if plan == nil { await read() } }
        .onChange(of: run.result) { _, done in
            guard let done, !done.unitsPending else { return }
            Task { await wizard.moveOn(model) }
        }
    }

    @ViewBuilder private func primary(_ run: StreamRun<MigrationDone>) -> some View {
        if let plan, !plan.conflicts.isEmpty || WizardWording.moveBlocked(plan) != nil {
            Button("Check Again") { Task { await read() } }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(reading)
        } else if let done = run.result, done.unitsPending {
            Button("Continue") { Task { await wizard.moveOn(model) } }
            Button("Finish Moving") { model.migrate() }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
        } else {
            Button(run.failure == nil ? "Move Files" : "Try Again") { model.migrate() }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(plan == nil || run.isBusy || run.result != nil)
        }
    }

    private func summary(_ plan: MigrationPlan) -> some View {
        WizardSection(title: "What Moves") {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(WizardWording.moveSummary(plan), id: \.self) { line in
                    Label(line, systemImage: "arrow.right.circle")
                        .labelStyle(.titleAndIcon)
                }
                if let blocked = WizardWording.moveBlocked(plan) {
                    StatusLine(tone: .warning, text: blocked)
                }
                if !plan.moves.isEmpty {
                    DisclosureGroup("Show Every Move") {
                        ScrollView {
                            VStack(alignment: .leading, spacing: 3) {
                                ForEach(Array(plan.moves.enumerated()), id: \.offset) { _, move in
                                    Text(Self.line(move))
                                        .font(.caption.monospaced())
                                        .foregroundStyle(.secondary)
                                        .textSelection(.enabled)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                }
                            }
                        }
                        .frame(maxHeight: 110)
                    }
                    .font(.callout)
                }
            }
        }
    }

    private func conflicts(_ plan: MigrationPlan) -> some View {
        WizardSection(title: "In the Way") {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(plan.conflicts.enumerated()), id: \.offset) { _, conflict in
                    ErrorText(StreamFailure(conflict.message, detail: conflict.detail))
                }
                Text("Nothing moves until it is settled. Check Again once it is.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder private func progress(_ run: StreamRun<MigrationDone>) -> some View {
        switch run.phase {
        case let .running(activity)?:
            WizardSection(title: "Moving", state: .working) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(activity.message).font(.callout)
                    ProgressView().progressViewStyle(.linear)
                }
            }
        case .cancelling?:
            WizardSection(title: "Moving", state: .working) {
                StatusLine(tone: .working, text: "Stopping…")
            }
        case let .failed(failure)?:
            WizardSection(title: "Moving") { ErrorText(failure) }
        case let .finished(done)?:
            WizardSection(title: "Moving", state: done.unitsPending ? .open : .done) {
                StatusLine(tone: done.unitsPending ? .warning : .ok, text: done.summary)
            }
        case nil:
            EmptyView()
        }
    }

    /// "~/.jarvis/pin → ~/.config/keryx/pin".
    static func line(_ move: MigrationMove) -> String {
        let source = WizardPaths.short(move.source)
        guard let destination = move.destination else { return "\(source) is removed" }
        return "\(source) → \(WizardPaths.short(destination))"
    }

    private func read() async {
        reading = true
        defer { reading = false }
        switch await model.migrationPlan() {
        case let .success(found):
            plan = found
            readError = nil
            // Nothing left to move after all: on to what is.
            if found.nothingToDo, model.migrationRun.phase == nil { await wizard.moveOn(model) }
        case let .failure(error):
            readError = error
        }
    }
}
