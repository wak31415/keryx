import KeryxKit
import SwiftUI

/// This Mac's LLM: where the voice on this Mac takes its words from (`VOICE_LLM`) — the
/// local agent's model, a model of its own on a server of its own here, or a server
/// elsewhere — as rows of the This Mac section. Picking a source shows its rows; nothing is
/// saved until its button (`keryx voice llm …`), which downloads, starts and checks what it
/// needs first, and moves a voice server running here onto it.
struct VoiceLLMRows: View {
    let voice: VoiceStatus
    let report: ModelsReport?

    @Environment(AppModel.self) private var model
    /// The source picked on screen; `nil` until one is, which shows the one in use.
    @State private var picked: VoiceLLMSource?
    /// The model picked for a server of its own; `nil` keeps the one in use (or the CLI's
    /// recommendation).
    @State private var hereModel: String?
    @State private var address: String
    @State private var remoteModel: String
    /// The key typed in the sheet, until Check and Use sends it on stdin. Never shown.
    @State private var key: String?
    /// Remove Key was chosen: Check and Use removes the stored key.
    @State private var clearing = false
    @State private var editingKey = false

    init(voice: VoiceStatus, report: ModelsReport?, picked: VoiceLLMSource? = nil) {
        self.voice = voice
        self.report = report
        _picked = State(initialValue: picked)
        let remote = voice.llmSource == .remote ? voice.llm : nil
        _address = State(initialValue: remote?.url ?? "")
        _remoteModel = State(initialValue: remote?.model ?? "")
    }

    var body: some View {
        let chosen = picked ?? voice.llmSource
        let run = model.voiceLLMRun
        LabeledContent {
            Picker("LLM", selection: Binding { chosen } set: { picked = $0 }) {
                ForEach(VoiceLLMSource.allCases) { source in
                    Text(VoiceLLMWording.title(source)).tag(source)
                }
            }
            .labelsHidden()
            .fixedSize()
            .disabled(run.isBusy)
        } label: {
            Text("LLM")
            Text(VoiceLLMWording.caption(chosen, inUse: voice.llmSource))
        }
        // Leaving with a server elsewhere typed in commits it, as a field does on losing focus.
        .onDisappear { commitRemote() }
        switch chosen {
        case .shared: sharedRows
        case .here: hereRows
        case .remote: remoteRows
        }
        Group {
            if let display = RunDisplay.of(run, finished: { VoiceLLMWording.finished($0, models: report?.models) }) {
                progress(run, display: display)
            } else {
                primary(chosen)
            }
        }
        // Saved: secrets.toml has the key now, and nothing need be held here.
        .onChange(of: run.result) { _, done in
            if done != nil {
                key = nil
                clearing = false
            }
        }
    }

    // MARK: - The local agent's model

    @ViewBuilder private var sharedRows: some View {
        let agent = report?.servers.localAgent.model
        LabeledContent {
            VStack(alignment: .trailing, spacing: 2) {
                Text(agent.map(title) ?? "None yet")
                    .foregroundStyle(agent == nil ? .secondary : .primary)
                    .textSelection(.enabled)
                if agent != nil, let server = VoiceWording.wordsServer(report) {
                    HStack(spacing: 6) {
                        StatusDot(server.running ? .ok : .inactive)
                        Text(server.text)
                    }
                    .font(.callout)
                    .foregroundStyle(.secondary)
                }
            }
        } label: {
            Text("Model")
            Text(agent == nil ? VoiceLLMWording.noAgentModel : VoiceLLMWording.sharedCaption)
        }
    }

    // MARK: - A model of its own, here

    @ViewBuilder private var hereRows: some View {
        let models = report?.models ?? []
        let current = voice.llmSource == .here ? voice.llm?.model : nil
        let selected = hereModel ?? current ?? report?.voiceLlmRecommended
        LabeledContent {
            Picker("Model", selection: Binding { selected ?? "" } set: { hereModel = $0.isEmpty ? nil : $0 }) {
                if selected == nil { Text("Choose a Model").tag("") }
                ForEach(VoiceLLMWording.choices(models, current: current)) { entry in
                    Text(VoiceLLMWording.menuTitle(entry, recommended: report?.voiceLlmRecommended)).tag(entry.name)
                }
            }
            .labelsHidden()
            .fixedSize()
            .disabled(model.voiceLLMRun.isBusy)
        } label: {
            Text("Model")
            Text(VoiceLLMWording.hereCaption(models.first { $0.name == selected }))
        }
        if voice.llmSource == .here, let llm = voice.llm, llm.model != nil {
            serverRow(llm)
        }
    }

    /// The voice LLM's own server: whether it serves its model. Started and stopped in Local
    /// Models, which owns the model servers; a way there while it isn't serving.
    @ViewBuilder private func serverRow(_ llm: VoiceLLMStatus) -> some View {
        let server = model.voiceLLMServerRun
        LabeledContent {
            HStack(spacing: 8) {
                if server.isBusy {
                    ProgressView().controlSize(.small)
                    Text(server.activity.map { VoiceWording.running($0.message) } ?? "Stopping…")
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                } else {
                    StatusDot(llm.reachable == true ? .ok : .inactive)
                    Text(VoiceLLMWording.serverState(llm))
                        .textSelection(.enabled)
                    if llm.reachable != true {
                        Button("Open Local Models") { model.show(inModels: ModelsAnchor.voiceLLMServer) }
                    }
                }
            }
        } label: {
            Text("Server")
            Text(llm.unitInstalled ? "Starts at login" : "Not installed")
        }
    }

    // MARK: - A server elsewhere

    @ViewBuilder private var remoteRows: some View {
        let busy = model.voiceLLMRun.isBusy
        LabeledContent {
            TextField("Address", text: $address, prompt: Text("https://api.openai.com/v1"))
                .onSubmit { commitRemote() }
                .labelsHidden()
                .textFieldStyle(.roundedBorder)
                .multilineTextAlignment(.leading)
                .frame(width: 280)
                .disabled(busy)
        } label: {
            Text("Address")
            Text("A Responses API: a box of yours, or a cloud's")
        }
        LabeledContent {
            TextField("Model", text: $remoteModel, prompt: Text("gpt-5-mini"))
                .onSubmit { commitRemote() }
                .labelsHidden()
                .textFieldStyle(.roundedBorder)
                .multilineTextAlignment(.leading)
                .frame(width: 280)
                .disabled(busy)
        } label: {
            Text("Model")
            Text("The server's own name for it")
        }
        LabeledContent {
            HStack(spacing: 8) {
                Text(VoiceWording.serverKeyState(typed: key != nil, clearing: clearing, stored: keySet))
                    .foregroundStyle(.secondary)
                if clearing {
                    Button("Keep Key") { clearing = false }
                } else if keySet, key == nil {
                    Button("Remove Key") { clearing = true }
                }
                Button(keySet || key != nil ? "Change Key…" : "Set Key…") { editingKey = true }
                    .accessibilityLabel("Set the Voice LLM's API Key")
            }
            .disabled(busy)
        } label: {
            Text("API key")
            Text(VoiceLLMWording.keyCaption)
        }
        .sheet(isPresented: $editingKey) {
            SecretSheet(key: "VOICE_LLM_API_KEY", title: "Voice LLM API key", replacing: keySet, typed: "",
                        footnote: VoiceWording.keyFootnote, keep: { typed in
                            let trimmed = typed.trimmingCharacters(in: .whitespacesAndNewlines)
                            key = trimmed.isEmpty ? nil : trimmed
                            if key != nil { clearing = false }
                        })
        }
        if voice.llmSource == .remote, let llm = voice.llm, let state = VoiceLLMWording.remoteState(llm) {
            LabeledContent("Server") {
                HStack(spacing: 6) {
                    StatusDot(state.tone)
                    Text(state.text)
                }
            }
        }
    }

    private var keySet: Bool {
        model.config.value?.first { $0.key == "VOICE_LLM_API_KEY" }?.set ?? false
    }

    // MARK: - Saving it

    /// The row that saves the source picked — or nothing, when it is the one in use.
    @ViewBuilder private func primary(_ chosen: VoiceLLMSource) -> some View {
        if !inUse(chosen) {
            let choice = choice(chosen)
            HStack(spacing: 8) {
                if let reason = VoiceLLMWording.cannot(chosen, report: report, voice: voice) {
                    Text(reason)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                Button(VoiceLLMWording.primary(chosen)) { if let choice { start(choice) } }
                    .disabled(choice == nil || !model.canChangeVoiceLLM
                              || VoiceLLMWording.cannot(chosen, report: report, voice: voice) != nil)
                    .help(model.voiceSetup.isBusy ? "Wait for the voice setup to finish" : "")
            }
        }
    }

    /// What the button asks for, from what is on screen; `nil` until it is enough.
    private func choice(_ chosen: VoiceLLMSource) -> VoiceLLMChoice? {
        switch chosen {
        case .shared:
            return .shared
        case .here:
            let current = voice.llmSource == .here ? voice.llm?.model : nil
            return (hereModel ?? current ?? report?.voiceLlmRecommended).map { .here(model: $0) }
        case .remote:
            let url = address.trimmingCharacters(in: .whitespacesAndNewlines)
            let name = remoteModel.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !url.isEmpty, !name.isEmpty else { return nil }
            return .remote(url: url, model: name, key: VoiceWording.serverKey(typed: key, clearing: clearing))
        }
    }

    /// The source on screen is the one in use, as it is set.
    private func inUse(_ chosen: VoiceLLMSource) -> Bool {
        guard chosen == voice.llmSource, let choice = choice(chosen) else { return false }
        return VoiceLLMWording.inUse(choice, voice: voice)
    }

    /// Return in a remote field, or leaving the screen with one typed in: Check and Use, when
    /// Remote or Cloud is picked, both fields are filled and they are not what is in use.
    private func commitRemote() {
        let chosen = picked ?? voice.llmSource
        guard chosen == .remote, let choice = choice(.remote), !inUse(.remote), model.canChangeVoiceLLM else { return }
        start(choice)
    }

    private func start(_ choice: VoiceLLMChoice) {
        model.voiceLLMRun.dismiss()
        model.chooseVoiceLLM(choice)
    }

    /// Try Again repeats the last choice, but not a remote one: its key, when one was typed,
    /// is not kept, so Check and Use asks again.
    private func again(_ choice: VoiceLLMChoice?) -> VoiceLLMChoice? {
        if case .remote? = choice { return nil }
        return choice
    }

    // MARK: - Under way

    @ViewBuilder private func progress(_ run: StreamRun<VoiceLLMChosen>, display: RunDisplay) -> some View {
        let lastChoice = model.voiceLLMChoice
        if case let .failed(failure)? = run.phase, let refusal = LocalRefusal.of(failure) {
            RunGuidanceRow(title: VoiceLLMWording.guidanceTitle(refusal, assistant: assistant),
                           message: VoiceLLMWording.guidanceDetail(refusal)) {
                Button("Dismiss") { run.dismiss() }
                if let choice = again(lastChoice) {
                    Button("Try Again") { start(choice) }
                }
            }
        } else {
            let order = RunSteps.voiceLLM(lastChoice.map(VoiceLLMWording.source) ?? voice.llmSource,
                                          seen: run.seenSteps)
            StreamRunView(groups: [RunStepGroup(steps: RunSteps.steps(order, reached: run.lastStep,
                                                                      stage: RunSteps.stage(run.phase)))],
                          display: display, cancel: { run.cancel() },
                          failureTitle: VoiceLLMWording.failed, paths: model.status.value?.paths,
                          again: ("Dismiss", { run.dismiss() })) {
                Button("Dismiss") { run.dismiss() }
                if let choice = again(lastChoice) {
                    Button("Try Again") { start(choice) }
                }
            }
        }
    }

    private var assistant: String? {
        Wording.assistant(model.status.value) ?? (voice.assistant.isEmpty ? nil : voice.assistant)
    }

    private func title(_ name: String) -> String {
        report?.models.first { $0.name == name }?.title ?? name
    }
}

/// How the This Mac section says where the voice's words come from.
@MainActor enum VoiceLLMWording {
    static func title(_ source: VoiceLLMSource) -> String {
        switch source {
        case .shared: "Same as Local Agent"
        case .here: "On This Mac"
        case .remote: "Remote or Cloud"
        }
    }

    /// Under the LLM's title: what it is, and — for one picked but not saved — that the one
    /// in use stays until its button is chosen.
    static func caption(_ chosen: VoiceLLMSource, inUse: VoiceLLMSource) -> String {
        let what = "The model that puts the replies together"
        guard chosen != inUse else { return what }
        return "\(title(inUse)) stays in use until you choose \(primary(chosen))"
    }

    static let sharedCaption = "The local agent's model, on its own server"
    static let noAgentModel = "The local agent has no model yet: choose one in Local Models"
    static let keyCaption = "Only if the server asks for one; OpenAI's own address uses your OpenAI key"

    /// The button that saves a source.
    static func primary(_ source: VoiceLLMSource) -> String {
        switch source {
        case .shared: "Use Local Agent's Model"
        case .here: "Use This Model"
        case .remote: "Check and Use"
        }
    }

    /// Why the button can't be chosen yet; `nil` when it can.
    static func cannot(_ source: VoiceLLMSource, report: ModelsReport?, voice: VoiceStatus) -> String? {
        switch source {
        case .shared:
            return report?.servers.localAgent.model == nil ? "Choose a local model in Local Models first." : nil
        case .here, .remote:
            return nil
        }
    }

    /// The models a server of its own may run: the catalog, smallest first. Which fit is the
    /// CLI's to say (`fits_with_voice_llm`, marked in `menuTitle`); the agent's `fits` answers
    /// another question and is never read here.
    static func choices(_ models: [CatalogModel], current: String?) -> [CatalogModel] {
        models.sorted { $0.needsGb < $1.needsGb }
    }

    /// "Qwen3.5 4B — Recommended, Fits", "… — Downloaded": the CLI's recommendation, its
    /// `fits_with_voice_llm` (no mark from a CLI without it), and whether it is on disk.
    static func menuTitle(_ entry: CatalogModel, recommended: String?) -> String {
        let marks = [entry.name == recommended ? "Recommended" : nil, entry.fitsWithVoiceLlm == true ? "Fits" : nil,
                     entry.downloaded ? "Downloaded" : nil].compactMap { $0 }
        return marks.isEmpty ? entry.title : "\(entry.title) — \(marks.joined(separator: ", "))"
    }

    /// Under the model's title: what choosing it does, and — when the CLI says — that it
    /// fits beside the agent's model and the speech models.
    static func hereCaption(_ entry: CatalogModel?) -> String {
        guard let entry else { return "A small model of its own answers fastest" }
        let memory = "needs about \(Formatting.gigabytes(entry.needsGb)) of memory"
        let fit = entry.fitsWithVoiceLlm == true ? ", and fits" : entry.fitsWithVoiceLlm == false ? ", more than is free" : ""
        return (entry.downloaded ? "Downloaded; \(memory)" : "\(Formatting.gigabytes(entry.sizeGb)) to download; \(memory)")
            + fit
    }

    /// The voice LLM's own server, in a few words.
    static func serverState(_ llm: VoiceLLMStatus) -> String {
        guard llm.reachable == true else { return llm.unitInstalled ? "Not serving its model" : "Not running" }
        return "Running at " + (llm.url.map(VoiceWording.address) ?? "127.0.0.1")
    }

    /// A server elsewhere, once it is the voice's: answering or not; nothing when not asked.
    static func remoteState(_ llm: VoiceLLMStatus) -> (tone: StatusDot.Tone, text: String)? {
        guard let reachable = llm.reachable else { return nil }
        return reachable ? (.ok, "Answering") : (.error, "Not answering")
    }

    /// `choice` is what the voice uses now, working: the same source, the same model, its
    /// own server serving it (else Use This Model is the way to download and start it), the
    /// same address with the key kept. A typed or removed key is a change.
    static func inUse(_ choice: VoiceLLMChoice, voice: VoiceStatus) -> Bool {
        guard source(choice) == voice.llmSource else { return false }
        switch choice {
        case .shared: return true
        case let .here(name): return name == voice.llm?.model && voice.llm?.reachable == true
        case let .remote(url, name, key):
            return sameAddress(url, voice.llm?.url) && name == voice.llm?.model && key == .stored
        }
    }

    /// Two addresses are the same one, but for a trailing slash or `/v1`.
    static func sameAddress(_ a: String, _ b: String?) -> Bool {
        func bare(_ url: String) -> String {
            var text = url.trimmingCharacters(in: .whitespaces).lowercased()
            while text.hasSuffix("/") { text.removeLast() }
            if text.hasSuffix("/v1") { text.removeLast(3) }
            return text
        }
        guard let b else { return false }
        return bare(a) == bare(b)
    }

    /// The source a choice asks for.
    static func source(_ choice: VoiceLLMChoice) -> VoiceLLMSource {
        switch choice {
        case .shared: .shared
        case .here: .here
        case .remote: .remote
        }
    }

    /// The row that says it worked: where the words come from now, and what became of the
    /// voice server here.
    static func finished(_ done: VoiceLLMChosen, models: [CatalogModel]?) -> (title: String, subtitle: String?) {
        let name = done.model.map { model in models?.first { $0.name == model }?.title ?? model }
        let title: String
        switch VoiceLLMSource(rawValue: done.source) {
        case .shared?: title = "The voice now uses the local agent's model"
        case .here?: title = "The voice now uses \(name ?? "its own model") on this Mac"
        case .remote?: title = "The voice now uses \(name ?? "a model") at \(done.url.map(VoiceWording.address) ?? "its server")"
        case nil: title = "The voice LLM was saved"
        }
        if done.voiceRestarted { return (title, "The voice server was restarted onto it, and answers.") }
        if done.voiceRestartNeeded { return (title, "The voice server takes it up when it starts.") }
        return (title, nil)
    }

    static let failed = "The voice LLM wasn't changed"

    /// A refusal said as guidance: its title.
    static func guidanceTitle(_ refusal: LocalRefusal, assistant: String?) -> String {
        switch refusal {
        case .liveCall: "\(assistant ?? "The assistant") is on a call"
        case .needsModel: VoiceWording.needsModelTitle
        case .needsServer: VoiceWording.serverDownTitle
        case .needsVoiceLLM: needsVoiceLLMTitle
        case .needsVoiceLLMServer: voiceLLMServerDownTitle
        }
    }

    static func guidanceDetail(_ refusal: LocalRefusal) -> String {
        switch refusal {
        case .liveCall: liveCallDetail
        case .needsModel: "The voice takes its words from the local agent's model. Choose one in Local Models."
        case .needsServer: VoiceWording.serverDownDetail
        case .needsVoiceLLM: needsVoiceLLMDetail
        case .needsVoiceLLMServer: voiceLLMServerDownDetail
        }
    }

    /// "Lyra is on a call. Try again when it ends.": a live-call refusal in one line.
    static func onACall(_ assistant: String?) -> String {
        "\(assistant ?? "The assistant") is on a call. Try again when it ends."
    }

    static let liveCallDetail = "Changing it restarts the voice server, which would cut the call off. "
        + "Try again when it ends."
    static let needsVoiceLLMTitle = "Choose the voice LLM first"
    static let needsVoiceLLMDetail = "The voice puts its replies together with it: choose one under LLM, then set up again."
    static let voiceLLMServerDownTitle = "The voice LLM's server isn't running"
    static let voiceLLMServerDownDetail = "The voice takes its words from it. Start it, then set up again."
}
