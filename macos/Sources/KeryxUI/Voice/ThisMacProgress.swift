import KeryxKit
import SwiftUI

/// What This Mac's Set Up… is doing, as rows of its section: every step of the model
/// server's start (when it began there) and of the voice setup, the one under way with its
/// words and a bar while it downloads, Cancel; then how it ended — set up, or what went
/// wrong, where a refusal that is a step to take first (a local model to choose, the model
/// server to start) is said as guidance with the action that takes it.
struct ThisMacProgress: View {
    /// Runs Set Up… again: `startServer` true to start the model server first whatever the
    /// models last said.
    let again: (_ startServer: Bool?) -> Void

    @Environment(AppModel.self) private var model

    /// Whether there is anything to show.
    static func showing(_ model: AppModel) -> Bool {
        model.voiceSetup.phase != nil
            || (model.thisMacSetup?.startsServer == true && model.modelServerRun.phase != nil)
    }

    var body: some View {
        let server = model.modelServerRun
        let setup = model.voiceSetup
        let state = Self.state(withServer: model.thisMacSetup?.startsServer == true, server: server.phase,
                               serverStep: server.lastStep, setup: setup.phase, setupStep: setup.lastStep)
        // A refusal tried nothing: there are no steps to show it at.
        if !Self.refused(state.ending) {
            RunStepList(groups: state.groups, message: state.message, progress: state.progress,
                        stopping: state.stopping)
        }
        switch state.ending {
        case .running:
            RunCancelRow(stopping: state.stopping) {
                if server.isRunning { server.cancel() } else { setup.cancel() }
            }
        case let .failed(message):
            failure(message)
        case let .finished(url):
            Label {
                Text("Set up at \(VoiceWording.address(url))")
                Text("Calls use it once Keryx restarts.")
            } icon: {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            }
        }
    }

    @ViewBuilder private func failure(_ message: String) -> some View {
        switch LocalRefusal.of(message) {
        case .needsServer:
            RunFailureRow(title: VoiceWording.serverDownTitle, message: VoiceWording.serverDownDetail, guidance: true) {
                Button("Dismiss") { model.dismissThisMacSetup() }
                Button("Start It and Set Up") { again(true) }
            }
        case .needsModel:
            let recommended = model.models.value?.models.first { $0.recommended }
            RunFailureRow(title: VoiceWording.needsModelTitle,
                          message: VoiceWording.needsModelDetail(recommended: recommended), guidance: true) {
                Button("Dismiss") { model.dismissThisMacSetup() }
                Button(VoiceWording.needsModelButton(recommended: recommended)) { model.showRecommendedModel() }
            }
        case nil:
            let said = RunLog.split(message)
            RunFailureRow(title: VoiceWording.setupFailed,
                          message: VoiceWording.sentence(Wording.named(said.text, titles: Wording.titles(model.config.value)))) {
                if let log = said.log {
                    Button("Show Log") { RunLog.show(log) }
                }
                Button("Dismiss") { model.dismissThisMacSetup() }
                Button("Try Again") { again(nil) }
            }
        }
    }

    /// It ended in a failure, whose own row offers what comes next.
    static func failed(_ model: AppModel) -> Bool {
        guard showing(model) else { return false }
        if case .failed = model.voiceSetup.phase { return true }
        return model.voiceSetup.phase == nil && model.modelServerRun.failure != nil
    }

    /// It ended on a refusal that is a step to take first.
    static func refused(_ ending: State.Ending) -> Bool {
        if case let .failed(message) = ending { return LocalRefusal.of(message) != nil }
        return false
    }

    // MARK: - What the two runs come to

    struct State: Equatable {
        enum Ending: Equatable {
            case running
            case failed(String)
            case finished(String)
        }

        var groups: [RunStepGroup]
        var message: String?
        var progress: (done: Int64, total: Int64?)?
        var stopping: Bool
        var ending: Ending

        static func == (a: State, b: State) -> Bool {
            a.groups == b.groups && a.message == b.message && a.stopping == b.stopping && a.ending == b.ending
                && a.progress?.done == b.progress?.done && a.progress?.total == b.progress?.total
        }
    }

    /// The two runs as one list. `withServer`: Set Up… began with the model server, whose
    /// run is `server` (at `serverStep`); the voice setup is `setup` (at `setupStep`), and
    /// its having begun means the model server's part is done.
    static func state(withServer: Bool, server: StreamRun<ModelServerStarted>.Phase?, serverStep: String,
                      setup: VoiceSetup.Phase?, setupStep: String) -> State {
        let setupStage = RunSteps.stage(setup)
        let voice = RunSteps.steps(RunSteps.voice, reached: setupStep, stage: setupStage)
        var groups = [RunStepGroup(title: withServer ? VoiceWording.voiceSteps : nil, steps: voice)]
        var message: String?
        var progress: (done: Int64, total: Int64?)?
        var stopping = setup == .cancelling
        var ending = State.Ending.running

        if withServer {
            let serverStage = setupStage == .notStarted ? RunSteps.stage(server) : .finished
            let steps = RunSteps.steps(RunSteps.modelServer, reached: serverStep, stage: serverStage)
            groups.insert(RunStepGroup(title: VoiceWording.serverSteps, steps: steps), at: 0)
            if setupStage == .notStarted {
                switch server {
                case let .running(activity):
                    message = activity.message
                    progress = activity.progress.map { ($0.done, $0.total) }
                case .cancelling: stopping = true
                case let .failed(failure): ending = .failed(failure)
                case .finished, nil: break
                }
            }
        }

        switch setup {
        case let .running(_, words, sizes):
            message = words
            // A total of 0 is a size the source did not say.
            progress = sizes.map { ($0.done, $0.total > 0 ? $0.total : nil) }
        case let .failed(failure): ending = .failed(failure)
        case let .finished(url): ending = .finished(url)
        case .cancelling, nil: break
        }
        return State(groups: groups, message: message, progress: progress, stopping: stopping, ending: ending)
    }
}
