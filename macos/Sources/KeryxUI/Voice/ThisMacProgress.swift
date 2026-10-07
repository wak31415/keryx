import KeryxKit
import SwiftUI

/// What This Mac's Set Up… is doing, as rows of its section: every step of the model
/// server's start (when it began there) and of the voice setup, the one under way with its
/// words and a bar while it downloads, Cancel; then how it ended — set up, or what went
/// wrong, where a refusal that is a step to take first (a local model to choose, the model
/// server to start) is said as guidance with the action that takes it.
struct ThisMacProgress: View {
    /// Runs Set Up… again: `startServer` true to start the model server first whatever the
    /// models last said, with `runtime` (`ollama`) when it is not llama.cpp.
    let again: (_ startServer: Bool?, _ runtime: String?) -> Void
    /// Set Up Again… once it has worked: the question Set Up… asks.
    var setUpAgain: () -> Void = {}

    @Environment(AppModel.self) private var model

    /// Whether there is anything to show.
    static func showing(_ model: AppModel) -> Bool {
        model.voiceSetup.phase != nil
            || (model.thisMacSetup?.startsServer == true && model.modelServerRun.phase != nil)
    }

    var body: some View {
        let server = model.modelServerRun
        let setup = model.voiceSetup
        let state = Self.state(withServer: model.thisMacSetup?.startsServer == true,
                               runtime: model.thisMacSetup?.runtime, server: server.phase,
                               serverStep: server.lastStep, setup: setup.phase, setupStep: setup.lastStep,
                               setupDetail: setup.stepDetail, serverDetail: server.activity?.detail)
        switch state.ending {
        case .running:
            StreamRunView(groups: state.groups,
                          display: .running(.init(message: state.message, detail: state.detail,
                                                  progress: state.progress, stopping: state.stopping)),
                          cancel: { model.cancelThisMacSetup() }) { EmptyView() }
        case let .failed(failure):
            if let refusal = LocalRefusal.of(failure.message) {
                guidance(refusal)
            } else {
                StreamRunView(groups: state.groups, display: .failed(failure), failureTitle: VoiceWording.setupFailed,
                              paths: model.status.value?.paths) {
                    Button("Dismiss") { model.dismissThisMacSetup() }
                    Button("Try Again") {
                        // The model server's part failed: start it again, as it was started.
                        let server = model.voiceSetup.phase == nil && model.thisMacSetup?.startsServer == true
                        again(server ? true : nil, server ? model.thisMacSetup?.runtime : nil)
                    }
                }
            }
        case let .finished(url):
            StreamRunView(groups: state.groups,
                          display: .finished(title: "Set up at \(VoiceWording.address(url))",
                                             subtitle: "Calls use it once Keryx restarts."),
                          again: ("Set Up Again…", setUpAgain)) { EmptyView() }
        }
    }

    /// A refusal that is a step to take first: say it as guidance, with the action that
    /// takes it. A refusal tried nothing, so there are no steps to show it at.
    @ViewBuilder private func guidance(_ refusal: LocalRefusal) -> some View {
        switch refusal {
        case .needsServer:
            needsServer(model.models.value?.wordsServer)
        case .needsModel:
            let recommended = model.models.value?.models.first { $0.recommended }
            RunGuidanceRow(title: VoiceWording.needsModelTitle,
                           message: VoiceWording.needsModelDetail(recommended: recommended)) {
                Button("Dismiss") { model.dismissThisMacSetup() }
                Button(VoiceWording.needsModelButton(recommended: recommended)) { model.showRecommendedModel() }
            }
        }
    }

    /// The voice refused because the server its words come from isn't answering: start it
    /// (this Mac's model server, or Ollama's copy of the model) and carry on — or, when it
    /// is somewhere Keryx does not start, say where, and try again once it answers.
    @ViewBuilder private func needsServer(_ server: WordsServer?) -> some View {
        switch server {
        case let .elsewhere(address):
            RunGuidanceRow(title: VoiceWording.serverDownTitle, message: VoiceWording.elsewhereDown(address)) {
                Button("Dismiss") { model.dismissThisMacSetup() }
                Button("Try Again") { again(false, nil) }
            }
        case .ollama:
            RunGuidanceRow(title: VoiceWording.ollamaDownTitle, message: VoiceWording.ollamaDownDetail) {
                Button("Dismiss") { model.dismissThisMacSetup() }
                Button("Start It and Set Up") { again(true, "ollama") }
            }
        case .modelServer, .noModel, nil:
            RunGuidanceRow(title: VoiceWording.serverDownTitle, message: VoiceWording.serverDownDetail) {
                Button("Dismiss") { model.dismissThisMacSetup() }
                Button("Start It and Set Up") { again(true, nil) }
            }
        }
    }

    /// It ended in a failure, whose own row offers what comes next.
    static func failed(_ model: AppModel) -> Bool {
        guard showing(model) else { return false }
        if case .failed = model.voiceSetup.phase { return true }
        return model.voiceSetup.phase == nil && model.modelServerRun.failure != nil
    }

    /// It worked, and its one row says so.
    static func finished(_ model: AppModel) -> Bool {
        if case .finished = model.voiceSetup.phase { return true }
        return false
    }

    /// It ended on a refusal that is a step to take first.
    static func refused(_ ending: State.Ending) -> Bool {
        if case let .failed(failure) = ending { return LocalRefusal.of(failure.message) != nil }
        return false
    }

    // MARK: - What the two runs come to

    struct State: Equatable {
        enum Ending: Equatable {
            case running
            case failed(StreamFailure)
            case finished(String)
        }

        var groups: [RunStepGroup]
        var message: String?
        var detail: String?
        var progress: (done: Int64, total: Int64?)?
        var stopping: Bool
        var ending: Ending

        static func == (a: State, b: State) -> Bool {
            a.groups == b.groups && a.message == b.message && a.detail == b.detail && a.stopping == b.stopping
                && a.ending == b.ending
                && a.progress?.done == b.progress?.done && a.progress?.total == b.progress?.total
        }
    }

    /// The two runs as one list. `withServer`: Set Up… began with the model server, whose
    /// run is `server` (at `serverStep`); the voice setup is `setup` (at `setupStep`), and
    /// its having begun means the model server's part is done.
    static func state(withServer: Bool, runtime: String? = nil, server: StreamRun<ModelServerStarted>.Phase?,
                      serverStep: String, setup: VoiceSetup.Phase?, setupStep: String,
                      setupDetail: String? = nil, serverDetail: String? = nil) -> State {
        let setupStage = RunSteps.stage(setup)
        let voice = RunSteps.steps(RunSteps.voice, reached: setupStep, stage: setupStage)
        var groups = [RunStepGroup(title: withServer ? VoiceWording.voiceSteps : nil, steps: voice)]
        var message: String?
        var detail: String?
        var progress: (done: Int64, total: Int64?)?
        var stopping = setup == .cancelling
        var ending = State.Ending.running

        if withServer {
            let serverStage = setupStage == .notStarted ? RunSteps.stage(server) : .finished
            let order = runtime == "ollama" ? RunSteps.ollama : RunSteps.modelServer
            let steps = RunSteps.steps(order, reached: serverStep, stage: serverStage)
            groups.insert(RunStepGroup(title: VoiceWording.serverSteps, steps: steps), at: 0)
            // The voice's steps are not to come once the model server's part has failed.
            if serverStage == .failed { groups.removeLast() }
            if setupStage == .notStarted {
                switch server {
                case let .running(activity):
                    message = activity.message
                    detail = activity.detail ?? serverDetail
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
            detail = setupDetail
            // A total of 0 is a size the source did not say.
            progress = sizes.map { ($0.done, $0.total > 0 ? $0.total : nil) }
        case let .failed(failure): ending = .failed(failure)
        case let .finished(url): ending = .finished(url)
        case .cancelling, nil: break
        }
        return State(groups: groups, message: message, detail: detail, progress: progress, stopping: stopping,
                     ending: ending)
    }
}
