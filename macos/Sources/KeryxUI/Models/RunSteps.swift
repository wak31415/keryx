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
        ("unit", "Set it to start in the background"),
        ("start", "Load the model"),
        ("check", "Check the model it serves"),
    ]

    /// `keryx models start --runtime ollama`: `runtime`, `start`, `check`, then `unit` when a
    /// llama.cpp server is left to retire.
    static let ollama: Order = [
        ("runtime", "Find Ollama"),
        ("start", "Pull the model into Ollama"),
        ("check", "Check Ollama lists it"),
        ("unit", "Retire the old llama.cpp server"),
    ]

    /// `keryx voice use here`: `install`, `download`, `configure`, `unit`, `start`.
    static let voice: Order = [
        ("install", "Install the voice server"),
        ("download", "Download the speech models"),
        ("configure", "Save the voice settings"),
        ("unit", "Set it to start in the background"),
        ("start", "Start the voice server"),
    ]

    /// `keryx voice use here`, with the steps a run announced (`seen`): a first set-up's
    /// voice LLM (`voice_llm`) comes before the rest, and only when it came.
    static func voice(seen: [String]) -> Order {
        seen.contains("voice_llm") ? [("voice_llm", "Set up the voice's own model")] + voice : voice
    }

    /// `keryx voice llm here NAME`: `download`, `runtime`, `unit`, `start`, `check`,
    /// `configure`.
    static let voiceLLMHere: Order = [
        ("download", "Download the model"),
        ("runtime", "Set up llama.cpp"),
        ("unit", "Set it to start in the background"),
        ("start", "Load the model"),
        ("check", "Check the model it serves"),
        ("configure", "Save the voice LLM"),
    ]

    /// `keryx voice llm remote URL`: `check`, `configure`.
    static let voiceLLMRemote: Order = [
        ("check", "Ask the server for its models"),
        ("configure", "Save the voice LLM"),
    ]

    /// `keryx voice llm shared`: `configure`.
    static let voiceLLMShared: Order = [("configure", "Save the voice LLM")]

    /// The steps `voice llm` takes only sometimes: a voice server running here moved onto
    /// the new words, the voice LLM it replaced brought back after a failure, its own server
    /// retired once nothing uses it.
    static let voiceLLMSometimes: [String: String] = [
        "voice": "Restart the voice server onto it",
        "restore": "Bring back the voice LLM it replaced",
        "unit": "Retire the voice LLM's own server",
    ]

    /// `keryx voice llm` for `source`, with the steps it takes only sometimes where they came.
    static func voiceLLM(_ source: VoiceLLMSource, seen: [String]) -> Order {
        let base = switch source {
        case .shared: voiceLLMShared
        case .here: voiceLLMHere
        case .remote: voiceLLMRemote
        }
        return order(base, sometimes: voiceLLMSometimes, seen: seen)
    }

    /// `base`, with each step of `sometimes` a run announced (`seen`) put after the step it
    /// came after — at the end, when that is none of them.
    static func order(_ base: Order, sometimes: [String: String], seen: [String]) -> Order {
        var order = base
        for (index, id) in seen.enumerated() where !order.contains(where: { $0.id == id }) {
            guard let title = sometimes[id] else { continue }
            let after = index > 0 ? order.firstIndex { $0.id == seen[index - 1] } : nil
            order.insert((id, title), at: after.map { $0 + 1 } ?? order.endIndex)
        }
        return order
    }

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
    /// What a terminal adds to it (a path), under Show Details.
    var detail: String?
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
            if let detail, !detail.isEmpty {
                DetailsDisclosure(detail)
                    .frame(maxWidth: 360, alignment: .leading)
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

/// Every long-running command, drawn one way (`models start`, `voice download`, `voice use
/// here`, This Mac's Set Up…): its steps as a checklist with the CLI's caption under the one
/// under way, a bar — determinate once the size is known — and Cancel; once it worked, one
/// row that says so (and, when given, the button that runs it again); once it failed, the
/// steps with a cross where it stopped and a row that says why in a sentence, with what the
/// CLI added under Show Details.
struct StreamRunView<Actions: View>: View {
    let groups: [RunStepGroup]
    let display: RunDisplay
    var cancel: () -> Void = {}
    /// The failure row's title ("The model server didn't start").
    var failureTitle = "It didn't finish"
    /// Keryx's paths, so a log the failure names can be offered (`RunLog`).
    var paths: Paths?
    /// The finished row's button, and what it does ("Set Up Again…").
    var again: (title: String, action: () -> Void)?
    /// The failure row's buttons (Dismiss, Try Again).
    @ViewBuilder var actions: () -> Actions

    var body: some View {
        switch display {
        case let .running(run):
            RunStepList(groups: groups, message: run.message, detail: run.detail, progress: run.progress,
                        stopping: run.stopping)
            RunCancelRow(stopping: run.stopping, cancel: cancel)
        case let .failed(failure):
            RunStepList(groups: groups)
            RunFailureRow(title: failureTitle, failure: failure, paths: paths, actions: actions)
        case let .finished(title, subtitle):
            RunSuccessRow(title: title, subtitle: subtitle, again: again)
        }
    }
}

/// Where a run is, as `StreamRunView` draws it.
enum RunDisplay {
    case running(RunProgress)
    case failed(StreamFailure)
    /// Worked: `title` in a row, `subtitle` under it.
    case finished(title: String, subtitle: String?)

    /// The steps of a run under way.
    struct RunProgress {
        var message: String?
        var detail: String?
        var progress: (done: Int64, total: Int64?)?
        var stopping = false
    }

    /// A `StreamRun`'s phase; `nil` before it has run.
    @MainActor static func of<Done>(_ run: StreamRun<Done>, finished: (Done) -> (title: String, subtitle: String?) = { _ in
        ("Done", nil)
    }) -> RunDisplay? {
        switch run.phase {
        case nil: nil
        case let .running(activity)?:
            .running(.init(message: activity.message, detail: activity.detail,
                           progress: activity.progress.map { ($0.done, $0.total) }))
        case .cancelling?:
            .running(.init(stopping: true))
        case let .failed(failure)?:
            .failed(failure)
        case let .finished(done)?:
            .finished(title: finished(done).title, subtitle: finished(done).subtitle)
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

/// The log a failure's detail names ("…; its log: /Users/you/…/keryx-llm.err.log", or an
/// older CLI's "(its log: …)" in the message): the text without it, and the file, so the
/// row can offer Show Log rather than print a path.
enum RunLog {
    static func split(_ message: String) -> (text: String, log: String?) {
        guard let match = message.range(of: #"(;\s*|\s*\()?its log: [^)\s;]+\)?"#, options: .regularExpression) else {
            return (message, nil)
        }
        let said = message[match]
        let path = said.range(of: "its log: ").map { said[$0.upperBound...] }?
            .trimmingCharacters(in: CharacterSet(charactersIn: ") ;")) ?? ""
        var text = message
        text.removeSubrange(match)
        return (text.trimmingCharacters(in: .whitespaces), path.isEmpty ? nil : path)
    }

    /// The log, when it is one Show Log may reveal: a `.log` file in Keryx's logs folder
    /// (`FileReveal`), never any path the message happened to name.
    static func file(_ path: String, paths: Paths?) -> URL? {
        FileReveal.log(path, paths: paths)
    }

    /// Selects the log in a Finder window; it is never opened from here.
    static func show(_ url: URL) {
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }
}

/// A run that went wrong, under its steps: a red cross, what happened in a sentence (the
/// title, then the CLI's message, in secondary colour — colour is for the icon), what the
/// CLI added under Show Details (with Show Log when it names Keryx's own log), and the
/// buttons that go on from it.
struct RunFailureRow<Actions: View>: View {
    let title: String
    let failure: StreamFailure
    var paths: Paths?
    @ViewBuilder let actions: () -> Actions

    var body: some View {
        let said = RunLog.split(failure.message)
        let detail = failure.detail.map(RunLog.split)
        let log = (said.log ?? detail?.log).flatMap { RunLog.file($0, paths: paths) }
        let more = [detail?.text].compactMap { $0 }.filter { !$0.isEmpty }.joined()
        VStack(alignment: .leading, spacing: 6) {
            LabeledContent {
                HStack(spacing: 8) {
                    if log != nil, more.isEmpty, let log {
                        Button("Show Log") { RunLog.show(log) }
                            .help("Shows the log in the Finder")
                    }
                    actions()
                }
            } label: {
                Label {
                    Text(title)
                    Text(Wording.sentence(said.text))
                        .textSelection(.enabled)
                } icon: {
                    Image(systemName: "xmark.octagon.fill")
                        .foregroundStyle(.red)
                        .accessibilityLabel("Failed")
                }
            }
            if !more.isEmpty {
                DetailsDisclosure(more, log: log)
                    .padding(.leading, 28)
            }
        }
    }
}

/// A refusal that is a step to take first (a model to choose, the model server to start),
/// not an error: an info symbol, what to do, and the button that does it.
struct RunGuidanceRow<Actions: View>: View {
    let title: String
    let message: String
    @ViewBuilder let actions: () -> Actions

    var body: some View {
        LabeledContent {
            HStack(spacing: 8) { actions() }
        } label: {
            Label {
                Text(title)
                Text(message)
            } icon: {
                Image(systemName: "info.circle.fill").foregroundStyle(.tint)
            }
        }
    }
}

/// A run that worked, collapsed to one row: a green check, what it did, and the button that
/// runs it again.
struct RunSuccessRow: View {
    let title: String
    var subtitle: String?
    var again: (title: String, action: () -> Void)?

    var body: some View {
        LabeledContent {
            if let again {
                Button(again.title, action: again.action)
            }
        } label: {
            Label {
                Text(title)
                if let subtitle { Text(subtitle) }
            } icon: {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                    .accessibilityLabel("Done")
            }
        }
    }
}
