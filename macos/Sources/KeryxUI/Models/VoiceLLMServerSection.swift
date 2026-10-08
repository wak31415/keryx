import KeryxKit
import SwiftUI

/// The voice LLM's own llama.cpp server (`dev.keryx.voice-llm`): the model it runs, whether
/// it serves it, its port, and Start (or Restart) and Stop — `keryx models start --voice-llm`
/// step by step, and `keryx service uninstall --voice-llm`. Which model it runs is chosen on
/// Voice, the one editor of the voice's words; while they are the local agent's, this says
/// so and shows the way there.
struct VoiceLLMServerSection: View {
    let report: ModelsReport

    @Environment(AppModel.self) private var model
    @State private var stopping = false
    @State private var failure: KeryxError?

    var body: some View {
        let voice = model.voice.value
        let llm = voice?.llm
        let unit = model.service.value?.units.voiceLlm
        let run = model.voiceLLMServerRun
        Section {
            if choosingHere { choosing(model.voiceLLMRun) }
            if let llm, VoiceLLMServerWording.isOwn(llm, unit: unit) {
                LabeledContent {
                    Text(llm.model.map(title) ?? "None chosen")
                        .foregroundStyle(llm.model == nil ? .secondary : .primary)
                        .textSelection(.enabled)
                } label: {
                    Text("Model")
                    Text(VoiceLLMServerWording.modelCaption(llm, voice: voice))
                }
                LabeledContent {
                    HStack(spacing: 6) {
                        StatusDot(llm.reachable == true ? .ok : .inactive)
                        Text(VoiceLLMServerWording.state(llm, port: port))
                            .textSelection(.enabled)
                    }
                } label: {
                    Text("Status")
                    Text(VoiceLLMServerWording.caption(unit: unit, llm: llm))
                }
                progress(run)
                if run.phase == nil || run.result != nil {
                    controls(llm, unit: unit)
                }
            } else if !choosingHere {
                LabeledContent {
                    Button("Open Voice") { model.selection = .voice }
                } label: {
                    Text(VoiceLLMServerWording.notInUseTitle(voice))
                    Text(VoiceLLMServerWording.notInUseDetail)
                }
            }
        } header: {
            Text("Voice LLM Server")
        }
        .id(ModelsAnchor.voiceLLMServer)
    }

    /// A model of its own being chosen for the voice (`voice llm here`, from Use This Model).
    private var choosingHere: Bool {
        guard model.voiceLLMRun.phase != nil, case .here? = model.voiceLLMChoice else { return false }
        return true
    }

    /// Its steps, how it ended, and what it did.
    @ViewBuilder private func choosing(_ run: StreamRun<VoiceLLMChosen>) -> some View {
        if case let .failed(failure)? = run.phase, let refusal = LocalRefusal.of(failure) {
            RunGuidanceRow(title: VoiceLLMWording.guidanceTitle(refusal, assistant: Wording.assistant(model.status.value)),
                           message: VoiceLLMWording.guidanceDetail(refusal)) {
                Button("Dismiss") { run.dismiss() }
            }
        } else if let display = RunDisplay.of(run, finished: { VoiceLLMWording.finished($0, models: report.models) }) {
            let order = RunSteps.voiceLLM(.here, seen: run.seenSteps)
            StreamRunView(groups: [RunStepGroup(steps: RunSteps.steps(order, reached: run.lastStep,
                                                                      stage: RunSteps.stage(run.phase)))],
                          display: display, cancel: { run.cancel() }, failureTitle: VoiceLLMWording.failed,
                          paths: model.status.value?.paths, again: ("Dismiss", { run.dismiss() })) {
                Button("Dismiss") { run.dismiss() }
            }
        }
    }

    /// A start under way, or one that went wrong; one that worked says so in Status.
    @ViewBuilder private func progress(_ run: StreamRun<ModelServerStarted>) -> some View {
        if case let .failed(failure)? = run.phase, LocalRefusal.of(failure) == .needsVoiceLLM {
            RunGuidanceRow(title: VoiceLLMWording.needsVoiceLLMTitle, message: VoiceLLMServerWording.chooseOnVoice) {
                Button("Dismiss") { run.dismiss() }
                Button("Open Voice") {
                    run.dismiss()
                    model.selection = .voice
                }
            }
        } else if let display = RunDisplay.of(run), case .finished = display {
            EmptyView()
        } else if let display = RunDisplay.of(run) {
            StreamRunView(groups: [RunStepGroup(steps: RunSteps.steps(RunSteps.modelServer, reached: run.lastStep,
                                                                      stage: RunSteps.stage(run.phase)))],
                          display: display, cancel: { run.cancel() },
                          failureTitle: VoiceLLMServerWording.failed, paths: model.status.value?.paths) {
                Button("Dismiss") { run.dismiss() }
                Button("Try Again") {
                    run.dismiss()
                    model.runVoiceLLMServer()
                }
            }
        }
    }

    @ViewBuilder private func controls(_ llm: VoiceLLMStatus, unit: UnitState?) -> some View {
        HStack(spacing: 8) {
            if stopping {
                ProgressView().controlSize(.small)
                Text("Stopping…").foregroundStyle(.secondary)
            } else if let failure {
                let said = VoiceWording.errorText(failure, operation: "The voice LLM's server didn't stop.",
                                                  titles: Wording.titles(model.config.value))
                VStack(alignment: .leading, spacing: 4) {
                    Text(said.message)
                        .problemStyle()
                        .textSelection(.enabled)
                        .lineLimit(3)
                    if let detail = said.detail { DetailsDisclosure(detail) }
                }
            }
            Spacer()
            if unit?.installed == true || llm.unitInstalled || llm.reachable == true {
                Button("Stop Voice LLM Server", action: stop)
                    .disabled(stopping || !model.canChangeVoiceLLM)
                    .help(model.voiceSetup.isBusy ? "Wait for the voice setup to finish" : "")
            }
            Button(llm.reachable == true ? "Restart Voice LLM Server" : "Start Voice LLM Server") {
                failure = nil
                model.runVoiceLLMServer()
            }
            .disabled(stopping || !model.canChangeVoiceLLM)
            .help(model.voiceSetup.isBusy ? "Wait for the voice setup to finish" : "")
        }
    }

    /// `VOICE_LLM_SERVER_PORT`, as set; else the one its address names.
    private var port: Int? {
        if case let .number(value)? = model.config.value?.first(where: { $0.key == "VOICE_LLM_SERVER_PORT" })?.value {
            return Int(value)
        }
        return nil
    }

    private func stop() {
        stopping = true
        failure = nil
        Task {
            if case let .failure(error) = await model.stopVoiceLLMServer() { failure = error }
            stopping = false
        }
    }

    private func title(_ name: String) -> String {
        report.models.first { $0.name == name }?.title ?? name
    }
}

/// How Local Models says what the voice LLM's own server is doing.
@MainActor enum VoiceLLMServerWording {
    /// The voice has a model of its own here, or its server is still installed: the section
    /// shows the server.
    static func isOwn(_ llm: VoiceLLMStatus, unit: UnitState?) -> Bool {
        llm.source == VoiceLLMSource.here.rawValue || llm.unitInstalled || unit?.installed == true
    }

    /// "Running at 127.0.0.1:8091", "Not running".
    static func state(_ llm: VoiceLLMStatus, port: Int?) -> String {
        guard llm.reachable == true else { return llm.unitInstalled ? "Not serving its model" : "Not running" }
        let address = llm.url.map(VoiceWording.address) ?? port.map { "127.0.0.1:\($0)" } ?? "127.0.0.1"
        return "Running at \(address)"
    }

    /// Under the status: whether its service is installed and starts at login.
    static func caption(unit: UnitState?, llm: VoiceLLMStatus) -> String {
        let installed = unit?.installed ?? llm.unitInstalled
        return installed ? "Its own llama.cpp server · starts at login" : "Its own llama.cpp server · not installed"
    }

    /// Under the model: in use by the voice, or left running while the voice's words come
    /// from elsewhere.
    static func modelCaption(_ llm: VoiceLLMStatus, voice: VoiceStatus?) -> String {
        guard llm.source == VoiceLLMSource.here.rawValue else {
            return "Not the voice's words now: stop it to free its memory"
        }
        return voice?.kind == .here ? "The voice's words; chosen in Voice" : "Chosen in Voice, for a voice on this Mac"
    }

    /// While the voice has no model of its own here.
    static func notInUseTitle(_ voice: VoiceStatus?) -> String {
        switch voice?.llmSource {
        case .remote?: "The voice's words come from a server elsewhere"
        default: "The voice shares the local agent's model"
        }
    }

    static let notInUseDetail = "A model of its own answers faster: choose one in Voice, under LLM."
    static let chooseOnVoice = "Choose the voice's model in Voice, under LLM: Use This Model downloads and starts it."
    static let failed = "The voice LLM's server didn't start"
}
