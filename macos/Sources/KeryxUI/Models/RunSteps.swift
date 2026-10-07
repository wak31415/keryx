import AppKit
import KeryxKit
import SwiftUI

/// One step of a long-running command (`models start`, `voice download`, `voice use here`),
/// in the order the CLI takes them: done, under way, where it failed, or still to come.
struct RunStep: Identifiable, Equatable {
    enum State: Equatable { case done, current, failed, pending }

    let id: String
    let title: String
    var state: State
}

/// A run's steps under a heading, when a list holds more than one run's ("Model Server",
/// "Voice Server").
struct RunStepGroup: Identifiable, Equatable {
    var title: String?
    var steps: [RunStep]

    var id: String { title ?? steps.first?.id ?? "" }
}

/// Where one run (of the steps in a list) has got to.
enum RunStage: Equatable {
    case notStarted
    /// Running or stopping, at the step it has reached.
    case running
    case failed
    case finished
}

/// The steps each command takes, as the list says them, and how far a run has got.
@MainActor enum RunSteps {
    typealias Order = [(id: String, title: String)]

    /// `keryx models start`, with llama.cpp: `runtime`, `unit`, `start`, `check`.
    static let modelServer: Order = [
        ("runtime", "Set up llama.cpp"),
        ("unit", "Install the background service"),
        ("start", "Load the model"),
        ("check", "Check the model it serves"),
    ]

    /// `keryx voice use here`: `install`, `download`, `configure`, `unit`, `start`.
    static let voice: Order = [
        ("install", "Install the voice server"),
        ("download", "Download the speech models"),
        ("configure", "Save the voice settings"),
        ("unit", "Install the background service"),
        ("start", "Start the voice server"),
    ]

    /// `keryx voice download`: `install`, `download`.
    static let voiceDownload: Order = [
        ("install", "Install the voice server"),
        ("download", "Download the speech models"),
    ]

    /// The steps of `order` for a run at `stage` that has reached `reached` (the CLI's name
    /// for its step; empty before the first). Those before it are done — a step the CLI
    /// skipped (speech models already downloaded) is done by being passed — the one it is
    /// on is under way or failed, and the rest are still to come. A step this build does
    /// not know leaves the first step under way.
    static func steps(_ order: Order, reached: String, stage: RunStage) -> [RunStep] {
        let at = order.firstIndex { $0.id == reached } ?? 0
        return order.enumerated().map { index, step in
            let state: RunStep.State
            switch stage {
            case .notStarted: state = .pending
            case .finished: state = .done
            case .running: state = index < at ? .done : index == at ? .current : .pending
            case .failed: state = index < at ? .done : index == at ? .failed : .pending
            }
            return RunStep(id: step.id, title: step.title, state: state)
        }
    }

    /// The stage of a `StreamRun`'s phase.
    static func stage<Done>(_ phase: StreamRun<Done>.Phase?) -> RunStage {
        switch phase {
        case nil: .notStarted
        case .running, .cancelling: .running
        case .failed: .failed
        case .finished: .finished
        }
    }

    /// The stage of a voice setup's phase.
    static func stage(_ phase: VoiceSetup.Phase?) -> RunStage {
        switch phase {
        case nil: .notStarted
        case .running, .cancelling: .running
        case .failed: .failed
        case .finished: .finished
        }
    }

    /// The size line beside a download's bar: "1.4 of 2.7 GB" with a total, "9.4 MB so far"
    /// without one, nothing before the first byte.
    static func sizes(done: Int64, total: Int64?) -> String? {
        if let total, total > 0 { return ModelRow.progress(done: done, total: total) }
        return done > 0 ? "\(Formatting.bytes(done)) so far" : nil
    }
}

/// A run's steps as a checklist — a tick for each done, a spinner on the one under way
/// (with the CLI's words for it, and a bar while it downloads), a cross where it failed —
/// under a heading per run when there is more than one.
struct RunStepList: View {
    let groups: [RunStepGroup]
    /// What the step under way is doing, in the CLI's words.
    var message: String?
    /// Bytes so far in the step under way.
    var progress: (done: Int64, total: Int64?)?
    /// Asked to stop: the step under way says so instead.
    var stopping = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(groups) { group in
                VStack(alignment: .leading, spacing: 7) {
                    if let title = group.title {
                        Text(title)
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(.secondary)
                    }
                    ForEach(group.steps) { step in
                        row(step)
                    }
                }
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .contain)
    }

    private func row(_ step: RunStep) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            icon(step.state)
                .frame(width: 16)
            VStack(alignment: .leading, spacing: 4) {
                Text(step.title)
                    .foregroundStyle(step.state == .pending ? .secondary : .primary)
                if step.state == .current {
                    current
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityValue(Self.spoken(step.state))
    }

    @ViewBuilder private var current: some View {
        if stopping {
            Text("Stopping…")
                .font(.callout)
                .foregroundStyle(.secondary)
        } else {
            if let message, !message.isEmpty {
                Text(VoiceWording.running(message))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .textSelection(.enabled)
            }
            if let progress {
                HStack(spacing: 10) {
                    if let total = progress.total, total > 0 {
                        ProgressView(value: Double(min(progress.done, total)), total: Double(total))
                    } else {
                        ProgressView().progressViewStyle(.linear)
                    }
                    if let sizes = RunSteps.sizes(done: progress.done, total: progress.total) {
                        Text(sizes)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                            .fixedSize()
                    }
                }
                .frame(maxWidth: 360)
            }
        }
    }

    @ViewBuilder private func icon(_ state: RunStep.State) -> some View {
        switch state {
        case .done:
            Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
        case .current:
            ProgressView().controlSize(.small)
        case .failed:
            Image(systemName: "xmark.circle.fill").foregroundStyle(.red)
        case .pending:
            Image(systemName: "circle").foregroundStyle(.tertiary)
        }
    }

    private static func spoken(_ state: RunStep.State) -> String {
        switch state {
        case .done: "Done"
        case .current: "Under way"
        case .failed: "Failed"
        case .pending: "To come"
        }
    }
}

/// The row under a run's steps while it runs: Cancel, or that it is stopping.
struct RunCancelRow: View {
    let stopping: Bool
    let cancel: () -> Void

    var body: some View {
        HStack {
            Spacer()
            Button("Cancel", action: cancel)
                .disabled(stopping)
        }
    }
}

/// The log a failure names ("… (its log: /Users/you/…/keryx-llm.err.log)"): the message
/// without it, and the file, so the row can offer Show Log rather than print a path.
enum RunLog {
    static func split(_ message: String) -> (text: String, log: String?) {
        guard let match = message.range(of: #"\s*\(its log: [^)]+\)"#, options: .regularExpression) else {
            return (message, nil)
        }
        let path = message[match].drop { $0 != ":" }.dropFirst().dropLast()
            .trimmingCharacters(in: .whitespaces)
        var text = message
        text.removeSubrange(match)
        return (text, path.isEmpty ? nil : path)
    }

    /// Opens the log in the app that reads it (Console).
    static func show(_ path: String) {
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }
}

/// A run that went wrong, under its steps: what the CLI said as a sentence, and the buttons
/// that go on from it. `guidance` is a refusal that is a step to take first, not an error.
struct RunFailureRow<Buttons: View>: View {
    let title: String
    let message: String
    var guidance = false
    @ViewBuilder let buttons: () -> Buttons

    var body: some View {
        LabeledContent {
            HStack(spacing: 8) { buttons() }
        } label: {
            Label {
                Text(title)
                Text(message)
                    .foregroundStyle(guidance ? Color.secondary : Color.red)
                    .textSelection(.enabled)
            } icon: {
                Image(systemName: guidance ? "info.circle.fill" : "xmark.octagon.fill")
                    .foregroundStyle(guidance ? Color.accentColor : Color.red)
            }
        }
    }
}
