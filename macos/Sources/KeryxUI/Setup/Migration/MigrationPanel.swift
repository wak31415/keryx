import KeryxKit
import Observation
import SwiftUI

/// Moving Jarvis's files, the one way the app does it — the wizard's Move Your Files and the
/// main window's "Keryx Needs to Move Its Files" alike: what moving them does (`migrate
/// --json`), then the move itself (`migrate --yes --json`, `AppModel.migrationRun`). Once a
/// step has come there is no Cancel: the CLI finishes the move whatever it is told.
@Observable @MainActor
final class MigrationReader {
    private(set) var plan: MigrationPlan?
    private(set) var readError: KeryxError?
    private(set) var reading = false

    init(plan: MigrationPlan? = nil) {
        self.plan = plan
    }

    /// Reads the plan; `nothingToDo` runs when there is nothing left to move.
    func read(_ model: AppModel, nothingToDo: () async -> Void = {}) async {
        reading = true
        defer { reading = false }
        switch await model.migrationPlan() {
        case let .success(found):
            plan = found
            readError = nil
            if found.nothingToDo, model.migrationRun.phase == nil { await nothingToDo() }
        case let .failure(error):
            readError = error
        }
    }

    /// Something stands in the way: conflicts, or a call the move would cut off.
    var blocked: Bool {
        guard let plan else { return false }
        return !plan.conflicts.isEmpty || WizardWording.moveBlocked(plan) != nil
    }
}

/// What moves, what stands in the way, and how the move is going.
struct MigrationSections: View {
    let reader: MigrationReader

    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(spacing: 12) {
            if let plan = reader.plan {
                summary(plan)
                if !plan.conflicts.isEmpty { conflicts(plan) }
            } else if let readError = reader.readError {
                WizardSection(title: "What Moves") { ErrorText(readError.said("Keryx couldn't say what moves.")) }
            } else {
                WizardSection(title: "What Moves") {
                    StatusLine(tone: .working, text: "Looking at what Jarvis left…")
                }
            }
            progress(model.migrationRun)
        }
        // A conflict can appear only once the move starts: the plan read again says what it
        // is, and the button becomes Check Again.
        .onChange(of: model.migrationRun.failure) { _, failure in
            if failure != nil { Task { await reader.read(model) } }
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
        WizardSection(title: "Needs Your Decision") {
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
            WizardSection(title: "Moving") { ErrorText(Self.said(failure)) }
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

    /// The CLI's sentence for a move that stopped part way, in the words of the button that
    /// carries on; anything else as it is.
    static func said(_ failure: StreamFailure) -> StreamFailure {
        guard failure.message.hasPrefix("The migration stopped before it finished") else { return failure }
        return StreamFailure("The move stopped before it finished. Try Again picks up where it stopped.",
                             detail: failure.detail)
    }
}

/// The primary button: Check Again while something is in the way, Move Files, Try Again
/// after a failure, Finish Moving when the service is still to be put back. `continue`, the
/// wizard's: going on without finishing that.
struct MigrationPrimary: View {
    let reader: MigrationReader
    var check: () -> Void
    var proceed: (() -> Void)?

    @Environment(AppModel.self) private var model

    var body: some View {
        let run = model.migrationRun
        if reader.blocked {
            Button("Check Again") { check() }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(reader.reading)
        } else if let done = run.result, done.unitsPending {
            if let proceed { Button("Continue") { proceed() } }
            Button("Finish Moving") { model.migrate() }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
        } else {
            Button(run.failure == nil ? "Move Files" : "Try Again") { model.migrate() }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(reader.plan == nil || run.isBusy || run.result != nil)
        }
    }
}
