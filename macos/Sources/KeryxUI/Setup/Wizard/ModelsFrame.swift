import KeryxKit
import SwiftUI

/// Models: where Lyra's voice runs (OpenAI, this Mac, a server elsewhere), and who does the
/// work (Claude, Codex, a model on this Mac, a model server elsewhere). The choices are radio
/// cards, defaulted to what is set up now (OpenAI, Claude); only the chosen branch's controls
/// show. Continue needs each section answered — a key saved, a sign-in done, a model chosen —
/// not finished: a download started here is waited for on Checking.
package struct ModelsFrame: View {
    @Environment(AppModel.self) private var model
    @Environment(SetupWizard.self) private var wizard
    @State private var voicePlace: VoicePlace?
    @State private var workPlace: WorkPlace?
    /// The work's model on this Mac, and the voice's words model, as picked.
    @State private var workModel: String?
    @State private var wordsModel: String?
    /// This Mac's speech models, as picked; `nil` keeps what is set.
    @State private var hears: String?
    @State private var speaks: String?
    @State private var speaksIn: String?
    /// A model server elsewhere's model and harness, as picked.
    @State private var serverModel: String?
    @State private var harness: String?
    /// Pay with the plan's sign-in (true) or an API key, per cloud agent.
    @State private var usePlan: [WorkPlace: Bool] = [:]
    @State private var signingIn: SignInProvider?
    @State private var secret: SecretRequest?
    @State private var changing: Set<String> = []
    @State private var saving = false
    @State private var failure: KeryxError?

    /// The catalog's: the choices already made.
    package init(voice: VoicePlace? = nil, work: WorkPlace? = nil, changing: Set<String> = []) {
        _voicePlace = State(initialValue: voice)
        _workPlace = State(initialValue: work)
        _changing = State(initialValue: changing)
    }

    package var body: some View {
        let facts = WizardFacts(model)
        let who = WizardWording.assistant(facts, status: model.status.value)
        let state = state(facts)
        WizardPage(symbol: .system("cpu"), title: WizardWording.modelsTitle,
                   sentence: WizardWording.modelsSentence(who), position: wizard.position(facts)) {
            VStack(spacing: 10) {
                voiceSection(state, facts: facts, assistant: who)
                workSection(state, facts: facts)
                if let failure {
                    ErrorText(failure.said("The models weren't saved."))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        } trailing: {
            if saving { ProgressView().controlSize(.small) }
            Button("Back") { wizard.back() }
                .keyboardShortcut(.cancelAction)
                .disabled(saving)
            Button("Continue") { save(state, facts: facts) }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(!state.voiceAnswered || !state.workAnswered || saving)
        }
        .sheet(item: $secret) { request in
            SecretSheet(key: request.key, title: request.title, replacing: request.replacing)
        }
        .sheet(item: $signingIn, onDismiss: { Task { await model.refresh(.doctor, .config) } }) { provider in
            SignInSheet(kind: provider)
        }
        .task { await model.refresh(.voice, .models, .doctor, .config) }
    }

    // MARK: - Where things stand

    /// The frame's choices, and whether each section is answered.
    struct FrameState {
        var voice: VoicePlace
        var work: WorkPlace
        var workModel: String?
        var wordsModel: String?
        var voiceAnswered: Bool
        var workAnswered: Bool
        var voiceFolded: Bool
        var workFolded: Bool
    }

    private func state(_ facts: WizardFacts) -> FrameState {
        let voice = voicePlace ?? wizard.voicePlace ?? VoicePlace.current(model.voice.value)
        let work = workPlace ?? wizard.workPlace ?? WorkPlace.current(facts, models: model.models.value)
        let report = model.models.value
        let chosenWork = workModel ?? wizard.workModel ?? ModelsRules.workModel(report, voiceHere: voice == .thisMac)
        // The voice's words: its own model when that is a control (SEAM), else the work's
        // when it runs here, else the one picked for it.
        let words: String? = if ModelsRules.voiceWordsIsAControl(report, facts: facts) {
            wordsModel ?? ModelsRules.voiceWordsModel(report) ?? chosenWork ?? ""
        } else if work == .thisMac {
            chosenWork
        } else {
            wordsModel ?? wizard.voiceWordsModel ?? ModelsRules.workModel(report, voiceHere: true)
        }
        let keySaved = facts.row("OPENAI_API_KEY")?.set == true || model.voice.value?.openaiKeySet == true
        let voiceAnswered = ModelsRules.voiceAnswered(voice, voice: model.voice.value, keySaved: keySaved,
                                                      wordsModel: words)
        let workAnswered = ModelsRules.workAnswered(work, signedIn: signedIn(work),
                                                    keySaved: ModelsRules.keyFor(work).map { facts.row($0)?.set == true } ?? false,
                                                    model: work == .elsewhere ? (serverModel ?? facts.text("LOCAL_AGENT_MODEL"))
                                                        : chosenWork, facts: facts)
        let first = wizard.mode == .firstRun
        return FrameState(voice: voice, work: work, workModel: chosenWork, wordsModel: words,
                          voiceAnswered: voiceAnswered, workAnswered: workAnswered,
                          voiceFolded: first && facts.done("voice") && !changing.contains("voice"),
                          workFolded: first && facts.done("agents") && !changing.contains("work"))
    }

    /// The doctor says the agent is signed in, or this run's sign-in to it finished.
    private func signedIn(_ work: WorkPlace) -> Bool {
        guard let provider = work.provider else { return false }
        if model.signInRun.result?.name == provider.rawValue { return true }
        if case .signedIn = AgentSignInWording.state(provider, doctor: model.doctor.value) { return true }
        return false
    }

    // MARK: - The voice

    @ViewBuilder private func voiceSection(_ state: FrameState, facts: WizardFacts, assistant: String) -> some View {
        let title = "\(assistant)'s Voice"
        if state.voiceFolded {
            FoldedSection(title: title, summary: voiceSummary(state.voice)) { changing.insert("voice") }
        } else {
            let tight = ModelsRules.tight(model.models.value)
            WizardSection(title: title, state: state.voiceAnswered ? .done : .open) {
                VStack(alignment: .leading, spacing: 10) {
                    RadioCards(choices: VoicePlace.allCases,
                               selection: Binding { state.voice } set: { voicePlace = $0 },
                               title: WizardWording.voicePlace,
                               caption: { WizardWording.voiceCaption($0, tight: tight) })
                        .fixedSize(horizontal: false, vertical: true)
                    switch state.voice {
                    case .openai: openAIBranch(facts)
                    case .thisMac: thisMacVoice(state, facts: facts, assistant: assistant)
                    case .elsewhere: VoiceServerBranch(voice: model.voice.value)
                    }
                }
            }
        }
    }

    private func voiceSummary(_ place: VoicePlace) -> String {
        switch place {
        case .openai: "OpenAI, key saved"
        case .thisMac: "On this Mac"
        case .elsewhere: "A voice server at \(model.voice.value?.baseUrl.map(VoiceWording.address) ?? "its address")"
        }
    }

    private func openAIBranch(_ facts: WizardFacts) -> some View {
        let row = facts.row("OPENAI_API_KEY")
        let saved = row?.set == true || model.voice.value?.openaiKeySet == true
        return VStack(alignment: .leading, spacing: 6) {
            HStack {
                StatusLine(tone: saved ? .ok : .none, text: saved ? "OpenAI API key saved" : "No OpenAI API key yet")
                Spacer()
                Button(saved ? "Change Key…" : "Add Key…") {
                    secret = SecretRequest(key: "OPENAI_API_KEY", title: row?.displayTitle ?? "OpenAI API key",
                                           replacing: saved)
                }
                .controlSize(.regular)
            }
            Text(pipelineCaption(.openai, fallback: WizardWording.speechToSpeech))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    /// The way a mode speaks, as the CLI lists it (SEAM: `pipelines`), else `fallback`.
    private func pipelineCaption(_ mode: VoiceMode, fallback: String) -> String {
        switch model.voice.value?.pipelines(for: mode)?.first {
        case "three-model"?: WizardWording.threeModels
        case "speech-to-speech"?: WizardWording.speechToSpeech
        default: fallback
        }
    }

    @ViewBuilder private func thisMacVoice(_ state: FrameState, facts: WizardFacts, assistant: String) -> some View {
        let here = model.voice.value?.here
        let report = model.models.value
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 10) {
                column("Hears") {
                    OptionMenu(key: "VOICE_SERVER_STT", title: "Hears", current: hears ?? here?.stt ?? "") { hears = $0 }
                }
                column("Thinks") { thinks(state, facts: facts) }
                column("Speaks") {
                    OptionMenu(key: "VOICE_SERVER_TTS", title: "Speaks", current: speaks ?? here?.tts ?? "") { speaks = $0 }
                }
                column("In the voice") {
                    OptionMenu(key: "VOICE_SERVER_VOICE", title: "Voice", current: speaksIn ?? here?.voice ?? "") {
                        speaksIn = $0
                    }
                }
            }
            let words = state.wordsModel.flatMap { name in report?.models.first { $0.name == name } }
            Text(([report?.hardware].compactMap { $0 }.map { WizardWording.machine($0) + "." }
                  + [words.map(WizardWording.download), pipelineCaption(.here, fallback: "")])
                .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " "))
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// The voice's "Thinks" model: its own pop-up once the CLI has a setting for it (SEAM:
    /// `VOICE_LLM_MODEL`, the owner's rule from `voice_llm_separate`); the work's model when
    /// that runs here (today's shared behaviour); else the model its words come from.
    @ViewBuilder private func thinks(_ state: FrameState, facts: WizardFacts) -> some View {
        let report = model.models.value
        let fitting = ModelsRules.fitting(report)
        if ModelsRules.voiceWordsIsAControl(report, facts: facts) {
            Picker("Thinks", selection: Binding { state.wordsModel ?? "" } set: { wordsModel = $0 }) {
                Text(WizardWording.sameAsWork).tag("")
                ForEach(fitting) { Text($0.title).tag($0.name) }
            }
            .labelsHidden()
            if state.wordsModel?.isEmpty ?? true {
                Text(WizardWording.sharedWords(tight: report?.voiceLlmSeparate == false,
                                               assistant: WizardWording.assistant(facts)))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: 150, alignment: .leading)
            }
        } else if state.work == .thisMac {
            Text(WizardWording.sameAsWork)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .padding(.vertical, 3)
        } else {
            Picker("Thinks", selection: Binding { state.wordsModel ?? "" } set: { wordsModel = $0 }) {
                ForEach(fitting) { Text($0.title).tag($0.name) }
            }
            .labelsHidden()
        }
    }

    private func column<Content: View>(_ title: String, @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - The work

    @ViewBuilder private func workSection(_ state: FrameState, facts: WizardFacts) -> some View {
        if state.workFolded {
            FoldedSection(title: "The Work", summary: workSummary(state.work)) { changing.insert("work") }
        } else {
            WizardSection(title: "The Work", state: state.workAnswered ? .done : .open) {
                VStack(alignment: .leading, spacing: 10) {
                    RadioCards(choices: WorkPlace.allCases,
                               selection: Binding { state.work } set: { workPlace = $0 },
                               title: WizardWording.workPlace, caption: WizardWording.workCaption,
                               unavailable: unavailable)
                        .fixedSize(horizontal: false, vertical: true)
                    switch state.work {
                    case .claude, .codex: cloudAgent(state.work, facts: facts)
                    case .thisMac: localModel(state)
                    case .elsewhere: modelServer(facts)
                    }
                }
            }
        }
    }

    private func workSummary(_ place: WorkPlace) -> String {
        switch place {
        case .claude, .codex: "\(WizardWording.workPlace(place)), signed in"
        case .thisMac: "\(model.models.value?.servers.llm.model.map(modelTitle) ?? "A model") on this Mac"
        case .elsewhere: "A model server at \(model.models.value?.servers.localAgent.baseUrl ?? "its address")"
        }
    }

    private func modelTitle(_ name: String) -> String {
        model.models.value?.models.first { $0.name == name }?.title ?? name
    }

    /// A cloud agent whose SDK isn't in this copy of Keryx.
    private func unavailable(_ place: WorkPlace) -> String? {
        guard let provider = place.provider,
              AgentSignInWording.state(provider, doctor: model.doctor.value) == .notInstalled else { return nil }
        return "Not installed"
    }

    @ViewBuilder private func cloudAgent(_ place: WorkPlace, facts: WizardFacts) -> some View {
        let provider = place.provider ?? .claude
        let name = SignInWording.name(provider)
        let keyRow = ModelsRules.keyFor(place).flatMap(facts.row)
        let plan = usePlan[place] ?? true
        if signedIn(place) {
            let how: String? = if case let .signedIn(how) = AgentSignInWording.state(provider, doctor: model.doctor.value) {
                how
            } else { nil }
            HStack {
                StatusLine(tone: .ok, text: how.map { "Signed in · \(Wording.capitalised($0))" } ?? "Signed in to \(name)")
                Spacer()
                Button("Sign In Again…") { signingIn = provider }
                    .controlSize(.regular)
            }
        } else if keyRow?.set == true {
            HStack {
                StatusLine(tone: .ok, text: "\(keyRow?.displayTitle ?? "API key") saved")
                Spacer()
                Button("Change Key…") { askKey(place, row: keyRow) }
                    .controlSize(.regular)
            }
        } else {
            HStack(spacing: 10) {
                Picker("Pay with", selection: Binding { plan } set: { usePlan[place] = $0 }) {
                    Text("My Plan").tag(true)
                    Text("API Key").tag(false)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                if model.signInRun.isRunning, model.signingIn == provider {
                    StatusLine(tone: .working, text: "Waiting for your browser…")
                } else {
                    StatusLine(tone: .none, text: "Not signed in")
                }
                Spacer()
                if plan {
                    Button("Sign In…") { signingIn = provider }
                        .controlSize(.regular)
                } else {
                    Button("Add Key…") { askKey(place, row: keyRow) }
                        .controlSize(.regular)
                }
            }
        }
    }

    private func askKey(_ place: WorkPlace, row: ConfigRow?) {
        guard let key = ModelsRules.keyFor(place) else { return }
        secret = SecretRequest(key: key, title: row?.displayTitle ?? FixAction.humanised(key),
                               replacing: row?.set == true)
    }

    @ViewBuilder private func localModel(_ state: FrameState) -> some View {
        let report = model.models.value
        let fitting = ModelsRules.fitting(report)
        let chosen = state.workModel.flatMap { name in report?.models.first { $0.name == name } }
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                Picker("Model", selection: Binding { state.workModel ?? "" } set: { workModel = $0 }) {
                    ForEach(fitting) { entry in
                        Text(Self.label(entry)).tag(entry.name)
                    }
                }
                .labelsHidden()
                .frame(maxWidth: 230)
                if let chosen { downloadLine(chosen) }
                Spacer(minLength: 0)
                if let chosen, !chosen.downloaded, !downloading(chosen.name) {
                    Button("Download") { model.download(chosen.name) }
                        .controlSize(.regular)
                        .fixedSize()
                        .disabled(model.downloads.isBusy)
                }
            }
        }
    }

    /// "Qwen3-Coder 30B · Recommended, Tested, Downloaded": the badges the catalog gives it.
    static func label(_ entry: CatalogModel) -> String {
        let badges = [entry.recommended ? "Recommended" : nil, entry.tested ? "Tested" : nil,
                      entry.downloaded ? "Downloaded" : nil].compactMap { $0 }
        return badges.isEmpty ? entry.title : "\(entry.title) · \(badges.joined(separator: ", "))"
    }

    private func downloading(_ name: String) -> Bool {
        if case .running? = model.downloads.active[name] { return true }
        return model.downloads.active[name] == .cancelling
    }

    @ViewBuilder private func downloadLine(_ entry: CatalogModel) -> some View {
        switch model.downloads.active[entry.name] {
        case let .running(done, total)?:
            StatusLine(tone: .working, text: RunSteps.sizes(done: done, total: total) ?? "Downloading",
                       fraction: total > 0 ? Double(done) / Double(total) : nil)
                .frame(maxWidth: 170)
        case let .failed(failure)?:
            ErrorText(failure)
        default:
            StatusLine(tone: entry.downloaded ? .ok : .none,
                       text: entry.downloaded ? "Downloaded" : "\(Formatting.gigabytes(entry.sizeGb)) to download")
        }
    }

    /// A model server elsewhere: its address (saved by Check, so its models can be asked of
    /// it), a key on stdin, the model from the server's own list, and the harness.
    private func modelServer(_ facts: WizardFacts) -> some View {
        ModelServerBranch(facts: facts, model: Binding { serverModel ?? facts.text("LOCAL_AGENT_MODEL") ?? "" }
                            set: { serverModel = $0 },
                          harness: Binding { harness ?? facts.text("LOCAL_AGENT_API") ?? "" } set: { harness = $0 })
    }

    // MARK: - Continue

    private func save(_ state: FrameState, facts: WizardFacts) {
        saving = true
        failure = nil
        Task {
            defer { saving = false }
            let choice = ModelsChoice(voice: state.voice, work: state.work, hears: hears, speaks: speaks,
                                      speaksIn: speaksIn, wordsModel: state.wordsModel, serverModel: serverModel,
                                      harness: harness)
            let writes = ModelsRules.writes(choice, facts: facts, voice: model.voice.value, models: model.models.value)
            if let error = await WizardWrite.apply(writes, with: model) {
                failure = error
                return
            }
            wizard.voicePlace = state.voice
            wizard.workPlace = state.work
            wizard.workModel = state.work == .thisMac ? state.workModel : nil
            wizard.voiceWordsModel = state.voice == .thisMac && state.work != .thisMac ? state.wordsModel : nil
            startDownloads(state)
            await model.refresh(.voice, .models, .doctor)
            await wizard.moveOn(model)
        }
    }

    /// The downloads the choices need, started now so they run while the rest is asked:
    /// the model, and this Mac's speech models.
    private func startDownloads(_ state: FrameState) {
        let report = model.models.value
        let wanted = state.work == .thisMac ? state.workModel : state.voice == .thisMac ? state.wordsModel : nil
        if let wanted, let entry = report?.models.first(where: { $0.name == wanted }), !entry.downloaded,
           !model.downloads.isBusy {
            model.download(wanted)
        }
        if state.voice == .thisMac, model.voice.value?.here.modelsReady == false, model.canDownloadVoiceModels {
            model.downloadVoiceModels()
        }
    }
}

/// A model server elsewhere, compactly: the address and Check, which saves it and asks the
/// server for its models (`config options LOCAL_AGENT_MODEL`, live); the key, saved on
/// stdin by the secret sheet; the model and the harness, saved on Continue.
private struct ModelServerBranch: View {
    let facts: WizardFacts
    @Binding var model: String
    @Binding var harness: String

    @Environment(AppModel.self) private var app
    @State private var address = ""
    @State private var saving = false
    @State private var failure: KeryxError?
    @State private var editingKey = false

    var body: some View {
        let saved = facts.text("LOCAL_AGENT_BASE_URL")
        let trimmed = address.trimmingCharacters(in: .whitespacesAndNewlines)
        let list = app.options["LOCAL_AGENT_MODEL"]
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                TextField("Address", text: $address, prompt: Text("http://gpu-box:11434/v1"))
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(check)
                Button(facts.row("LOCAL_AGENT_API_KEY")?.set == true ? "Key Set" : "Key…") { editingKey = true }
                    .controlSize(.regular)
                Button("Check") { check() }
                    .controlSize(.regular)
                    .disabled(trimmed.isEmpty || saving)
            }
            if saved != nil {
                HStack(spacing: 12) {
                    LabeledContent("Model") {
                        OptionMenu(key: "LOCAL_AGENT_MODEL", title: "Model", current: model) { model = $0 }
                    }
                    .fixedSize()
                    LabeledContent("Harness") {
                        Picker("Harness", selection: $harness) {
                            if let row = facts.row("LOCAL_AGENT_API") {
                                ForEach(row.choices ?? [], id: \.self) { choice in
                                    Text(ConfigWording.choiceTitle(choice, for: row)).tag(choice)
                                }
                            }
                        }
                        .labelsHidden()
                        .fixedSize()
                    }
                    .fixedSize()
                    Spacer(minLength: 0)
                }
                .font(.callout)
            }
            if saving {
                StatusLine(tone: .working, text: "Asking \(VoiceWording.address(trimmed)) for its models…")
            } else if let failure {
                ErrorText(failure.said("The address wasn't saved."))
            } else if let note = OptionWording.note(list) {
                StatusLine(tone: .warning, text: note)
            } else if let saved, list?.value?.live == true {
                StatusLine(tone: .ok, text: "\(VoiceWording.address(saved)) answered")
            }
        }
        .onAppear { if address.isEmpty { address = saved ?? "" } }
        .sheet(isPresented: $editingKey) {
            SecretSheet(key: "LOCAL_AGENT_API_KEY", title: facts.row("LOCAL_AGENT_API_KEY")?.displayTitle ?? "API key",
                        replacing: facts.row("LOCAL_AGENT_API_KEY")?.set == true)
        }
    }

    /// Saves the address, then reads the server's models afresh.
    private func check() {
        let url = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !url.isEmpty, !saving else { return }
        saving = true
        failure = nil
        Task {
            defer { saving = false }
            if url != facts.text("LOCAL_AGENT_BASE_URL"), let error = await app.set(key: "LOCAL_AGENT_BASE_URL", to: url) {
                failure = error
                return
            }
            await app.loadOptions("LOCAL_AGENT_MODEL", force: true)
        }
    }
}

/// A voice server elsewhere: its address, a key kept in memory until it is sent on stdin,
/// and Check, which opens a Realtime session before it saves (`voice use server`). One that
/// did not answer is said in red, with Use It Anyway.
private struct VoiceServerBranch: View {
    let voice: VoiceStatus?

    @Environment(AppModel.self) private var model
    @State private var address = ""
    @State private var key: String?
    @State private var editingKey = false
    @State private var checking = false
    @State private var answer: VoiceServerResult?
    @State private var failure: KeryxError?

    var body: some View {
        let trimmed = address.trimmingCharacters(in: .whitespacesAndNewlines)
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                TextField("Address", text: $address, prompt: Text("http://gpu-box:8765"))
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { check(keepAnyway: false) }
                Button(key == nil ? "Key…" : "Key Set") { editingKey = true }
                    .controlSize(.regular)
                Button("Check") { check(keepAnyway: false) }
                    .controlSize(.regular)
                    .disabled(trimmed.isEmpty || checking)
            }
            .disabled(checking)
            if checking {
                StatusLine(tone: .working, text: "Opening a Realtime session…")
            } else if let answer {
                ForEach(answer.warnings, id: \.self) { StatusLine(tone: .warning, text: VoiceWording.sentence($0)) }
                if answer.saved != nil {
                    StatusLine(tone: .ok, text: "Saved")
                } else if let problem = answer.problem {
                    HStack {
                        StatusLine(tone: .problem, text: VoiceWording.sentence(problem))
                        Spacer()
                        Button("Use It Anyway") { check(keepAnyway: true) }
                            .controlSize(.regular)
                    }
                }
            } else if let failure {
                ErrorText(failure.said("The voice server wasn't checked."))
            } else if voice?.kind == .server {
                StatusLine(tone: .ok, text: "In use at \(voice?.baseUrl.map(VoiceWording.address) ?? "")")
            }
        }
        .onAppear { if address.isEmpty, voice?.kind == .server { address = voice?.baseUrl ?? "" } }
        .onChange(of: address) { answer = nil }
        .sheet(isPresented: $editingKey) {
            SecretSheet(key: "VOICE_API_KEY", title: "Voice server API key", replacing: false, typed: "",
                        footnote: VoiceWording.keyFootnote, keep: { typed in
                            let trimmed = typed.trimmingCharacters(in: .whitespacesAndNewlines)
                            key = trimmed.isEmpty ? nil : trimmed
                        })
        }
    }

    private func check(keepAnyway: Bool) {
        let url = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !url.isEmpty, !checking else { return }
        checking = true
        failure = nil
        Task {
            defer { checking = false }
            switch await model.useVoiceServer(url: url, key: VoiceWording.serverKey(typed: key, clearing: false),
                                              keepAnyway: keepAnyway) {
            case let .success(result):
                answer = result
                if result.saved != nil { key = nil }
            case let .failure(error):
                failure = error
            }
        }
    }
}
