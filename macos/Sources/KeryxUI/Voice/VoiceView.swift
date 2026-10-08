import KeryxKit
import SwiftUI

/// The Voice screen: how the assistant speaks — OpenAI Realtime, a voice server on this Mac,
/// or one elsewhere — and everything each needs. Picking a way shows its section; nothing
/// is saved until that section's button (Use OpenAI Realtime, Set Up…, Check & Use). Once
/// one is, the banner at the top offers the restart that loads it.
public struct VoiceView: View {
    /// This screen answers `request` (`RequestAnswer`): This Mac's Set Up… question.
    static func answers(_ request: AppRequest?) -> Bool {
        request.map(RequestAnswer.of) == .voiceHere
    }

    @Environment(AppModel.self) private var model
    /// The way picked on screen; `nil` until the owner picks one, which shows the one in use.
    @State private var picked: VoiceMode?
    private let server: ServerSection.Start
    /// Whether the Advanced disclosure starts open (the catalog's).
    private let advancedOpen: Bool
    /// The voice LLM's source picked when the screen appears (the catalog's).
    private let llm: VoiceLLMSource?

    public init() {
        self.init(mode: nil)
    }

    /// `mode`: the way picked when the screen appears; `server`: the server section's state
    /// then; `advancedOpen`: the Advanced disclosure open; `llm`: the voice LLM's source
    /// picked (the catalog's).
    package init(mode: VoiceMode?, server: ServerSection.Start = .init(), advancedOpen: Bool = false,
                 llm: VoiceLLMSource? = nil) {
        _picked = State(initialValue: mode)
        self.server = server
        self.advancedOpen = advancedOpen
        self.llm = llm
    }

    public var body: some View {
        StateView(model.voice, section: "Voice", retry: { Task { await model.refresh(.voice) } }) { voice in
            let chosen = picked ?? voice.kind ?? .openai
            Form {
                if model.configNeedsRestart {
                    RestartBanner()
                }
                ModeSection(voice: voice, chosen: Binding { chosen } set: { picked = $0 })
                switch chosen {
                case .openai: OpenAISection(voice: voice)
                case .here: HereSection(voice: voice, llmPicked: llm)
                case .server: ServerSection(voice: voice, start: server)
                }
                ScreenAdvancedSection(screen: .voice, open: advancedOpen)
            }
            .formStyle(.grouped)
        }
        .task {
            await model.refresh(.voice)
            // The words model's title, the API key rows and Advanced read these.
            if model.models.value == nil { await model.refresh(.models) }
            if model.config.value == nil { await model.refresh(.config) }
        }
        // The Setup Assistant's "Set Up Voice on This Mac" shows This Mac, whose section
        // asks the question (and says it has been answered).
        .onChange(of: model.requested, initial: true) { _, requested in
            if VoiceView.answers(requested) { picked = .here }
        }
    }
}

// MARK: - The modes

private struct ModeSection: View {
    let voice: VoiceStatus
    @Binding var chosen: VoiceMode

    @Environment(AppModel.self) private var model

    var body: some View {
        Section {
            Picker(selection: $chosen) {
                // Which is in use is the footer's to say: a mark beside one would knock its
                // radio button off the others' line.
                ForEach(VoiceMode.allCases) { mode in
                    Text(VoiceWording.title(mode)).tag(mode)
                }
            } label: {
                Text("Speak through")
            }
            .pickerStyle(.radioGroup)
            PipelineRow(voice: voice, mode: chosen)
        } header: {
            Text(VoiceWording.sectionTitle(Wording.assistant(model.status.value) ?? voice.assistant))
        } footer: {
            Text(VoiceWording.footer(chosen: chosen, inUse: voice.kind))
                .font(.callout)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// How the chosen way speaks (`voice --json` `pipelines`): a caption while it has one way, as
/// every way has today; the pipeline set, with the way to change it, once a way has several.
private struct PipelineRow: View {
    let voice: VoiceStatus
    let mode: VoiceMode

    @Environment(AppModel.self) private var model

    var body: some View {
        let ways = voice.pipelines(for: mode) ?? []
        if ways.count > 1 {
            let set = model.config.value?.first { $0.key == "VOICE_PIPELINE" }
            let current = set.flatMap { $0.value.map(ConfigWording.editText) }.flatMap { $0.isEmpty ? nil : $0 }
                ?? ways[0]
            LabeledContent {
                Button(ConfigWording.changeIn(.configuration)) { model.request(.setting(key: "VOICE_PIPELINE")) }
                    .buttonStyle(.link)
            } label: {
                Text("Pipeline")
                Text(VoiceWording.pipeline(current))
            }
        } else if let only = ways.first {
            Text("Pipeline: " + VoiceWording.pipeline(only) + ".")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }
}

// MARK: - OpenAI Realtime

private struct OpenAISection: View {
    let voice: VoiceStatus

    @Environment(AppModel.self) private var model
    @State private var editingKey = false
    @State private var saving = false
    @State private var failure: KeryxError?

    var body: some View {
        Section(VoiceWording.title(.openai)) {
            LabeledContent {
                HStack(spacing: 8) {
                    Text(voice.openaiKeySet ? "Set" : "Not set")
                        .foregroundStyle(.secondary)
                    Button(voice.openaiKeySet ? "Change Key…" : "Set Key…") { editingKey = true }
                        .accessibilityLabel(voice.openaiKeySet ? "Change OpenAI API Key" : "Set OpenAI API Key")
                }
            } label: {
                Text("API key")
            }
            SettingMenuRow(key: "OPENAI_REALTIME_MODEL", title: "Realtime model",
                           current: setting("OPENAI_REALTIME_MODEL") ?? voice.realtimeModel) {
                await model.refresh(.voice)
            }
            // Unset, it is the assistant's own voice: the setting says so, where the status
            // would name the voice that is.
            SettingMenuRow(key: "OPENAI_VOICE", title: "Voice", current: setting("OPENAI_VOICE") ?? voice.voice) {
                await model.refresh(.voice)
            }
            PrimaryRow(
                inUse: voice.kind == .openai, inUseTitle: "In Use", working: saving ? "Saving…" : nil,
                failure: failure.map {
                    VoiceWording.errorText($0, operation: "OpenAI Realtime wasn't chosen.",
                                           titles: Wording.titles(model.config.value))
                }
            ) {
                Button(VoiceWording.primary(.openai), action: use)
                    .disabled(saving || !voice.openaiKeySet)
                    .help(voice.openaiKeySet ? "" : "Set an OpenAI API key first")
            }
        }
        .sheet(isPresented: $editingKey, onDismiss: { Task { await model.refresh(.voice) } }) {
            SecretSheet(key: "OPENAI_API_KEY", title: "OpenAI API key", replacing: voice.openaiKeySet)
        }
    }

    /// A setting's value as Configuration has it, when it has been read.
    private func setting(_ key: String) -> String? {
        model.config.value?.first { $0.key == key }.map { ConfigWording.editText($0.value) }
    }

    private func use() {
        saving = true
        failure = nil
        Task {
            failure = await model.useVoiceOpenAI()
            saving = false
        }
    }
}

// MARK: - This Mac

private struct HereSection: View {
    let voice: VoiceStatus
    /// The voice LLM's source picked when it appears (the catalog's); `nil`: the one in use.
    var llmPicked: VoiceLLMSource?

    @Environment(AppModel.self) private var model
    /// The Kokoro voice picked here, applied by the next setup; `nil`: the one set.
    @State private var kokoro: String?
    @State private var confirming = false
    /// Restart Voice Server is reading the models before it decides what to start.
    @State private var reading = false
    /// A speech model was changed here since the voice server last started: the next start
    /// loads it.
    @State private var speechChanged = false

    var body: some View {
        let here = voice.here
        let setup = model.voiceSetup
        let report = model.models.value
        let inUse = voice.kind == .here
        let showing = ThisMacProgress.showing(model)
        // The model server starting on its own (Local Models'), not as this setup's part.
        let serverElsewhere = model.modelServerRun.isBusy && !showing
        let downloading = model.voiceDownloadRun.isBusy
        let busy = setup.isBusy || model.modelServerRun.isBusy
        let choosing = model.voiceLLMBusy
        // Set Up can't run without words: a local model while they are the agent's, a model
        // named for a voice LLM of its own.
        let wordless = voice.needsLocalModel || voice.needsVoiceLLM
        Section {
            if voice.needsLocalModel, !showing {
                needsModel(report)
            }
            LabeledContent {
                HStack(spacing: 6) {
                    StatusDot(here.reachable ? .ok : .inactive)
                    Text(VoiceWording.hereState(here))
                        .textSelection(.enabled)
                }
                .help(here.program ?? "")
            } label: {
                Text("Voice server")
                Text(VoiceWording.hereCaption(here))
            }
            SettingMenuRow(key: "VOICE_SERVER_STT", title: "Speech to text",
                           subtitle: VoiceWording.speechSubtitle("How it hears", changed: speechChanged, inUse: inUse),
                           current: here.stt) {
                speechChanged = true
                await model.refresh(.voice)
            }
                .disabled(busy || downloading)
            VoiceLLMRows(voice: voice, report: report, picked: llmPicked)
                .disabled(busy)
            SettingMenuRow(key: "VOICE_SERVER_TTS", title: "Text to speech",
                           subtitle: VoiceWording.speechSubtitle(VoiceWording.speechState(here), changed: speechChanged,
                                                                 inUse: inUse),
                           current: here.tts) {
                speechChanged = true
                await model.refresh(.voice)
            }
                .disabled(busy || downloading)
            LabeledContent {
                OptionMenu(key: "VOICE_SERVER_VOICE", title: "Kokoro voice", current: kokoro ?? here.voice) {
                    kokoro = $0
                }
                .disabled(busy)
            } label: {
                Text("Kokoro voice")
                if let kokoro, kokoro != here.voice {
                    Text("Takes effect when you choose \(inUse ? "Restart Voice Server" : "Set Up")")
                } else if let detail = OptionWording.detail(of: kokoro ?? here.voice,
                                                            in: model.options["VOICE_SERVER_VOICE"]?.value) {
                    Text(detail)
                }
            }
            if showing {
                ThisMacProgress(again: { startServer, runtime in start(startServer: startServer, runtime: runtime) },
                                setUpAgain: askToSetUp)
            }
            if serverElsewhere {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(VoiceWording.serverStartingElsewhere).foregroundStyle(.secondary)
                    Spacer()
                    Button("Open Local Models") { model.show(inModels: ModelsAnchor.modelServer) }
                }
            }
            // While a setup runs, or has failed, its own rows are the ones to act on.
            // …and once it has worked, its one row offers Set Up Again….
            if !busy, !ThisMacProgress.failed(model), !ThisMacProgress.finished(model) {
                PrimaryRow(inUse: false, inUseTitle: "", working: reading ? "Checking the model server…" : nil,
                           failure: nil) {
                    Button(inUse ? "Restart Voice Server" : "Set Up…") {
                        if inUse { restart() } else { askToSetUp() }
                    }
                    .disabled(wordless || downloading || reading || choosing)
                    .help(voice.needsLocalModel ? VoiceWording.needsModelTitle
                          : voice.needsVoiceLLM ? VoiceLLMWording.needsVoiceLLMTitle
                          : downloading ? VoiceWording.speechDownloading
                          : choosing ? VoiceWording.voiceLLMChanging : "")
                }
            }
        } header: {
            Text(VoiceWording.title(.here))
        } footer: {
            HStack(spacing: 4) {
                Text("Models are downloaded, and their servers run, in")
                    .foregroundStyle(.secondary)
                Button("Local Models") { model.show(inModels: ModelsAnchor.voiceModels) }
                    .buttonStyle(.link)
            }
            .font(.callout)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        // The voice picked stays picked until the setup that applies it has worked.
        .onChange(of: setup.phase) { _, phase in
            if case .finished = phase {
                kokoro = nil
                speechChanged = false
            }
            // A refusal may be over a model server the last read thought was up.
            if case .failed = phase { Task { await model.refresh(.models) } }
        }
        // The speech models' names, as their lists say them: held as a shown menu's are, so
        // a setup that changes the settings reads them again rather than dropping them.
        .task {
            await model.loadOptions("VOICE_SERVER_STT")
            await model.loadOptions("VOICE_SERVER_TTS")
        }
        .onAppear {
            model.optionsMenu("VOICE_SERVER_STT", shown: true)
            model.optionsMenu("VOICE_SERVER_TTS", shown: true)
        }
        .onDisappear {
            model.optionsMenu("VOICE_SERVER_STT", shown: false)
            model.optionsMenu("VOICE_SERVER_TTS", shown: false)
        }
        // The Setup Assistant's "Set Up Voice on This Mac": the same question as Set Up….
        .onChange(of: model.requested, initial: true) { _, requested in
            guard let requested, VoiceView.answers(requested) else { return }
            model.didHandle(requested)
            if !wordless, !busy, !downloading, !choosing { askToSetUp() }
        }
        .confirmationDialog(VoiceWording.setUpQuestion, isPresented: $confirming) {
            Button("Set Up") { start(startServer: nil) }
        } message: {
            Text(VoiceWording.setUpMessage(startsServer: (report?.modelServerDown ?? false)
                                                && model.voiceWordsAreTheAgents))
        }
    }

    /// No local model at all: what to do about it, and the way to the one to choose.
    private func needsModel(_ report: ModelsReport?) -> some View {
        let recommended = report?.models.first { $0.recommended }
        return LabeledContent {
            Button(VoiceWording.needsModelButton(recommended: recommended)) { model.showRecommendedModel() }
        } label: {
            Label {
                Text(VoiceWording.needsModelTitle)
                Text(VoiceWording.needsModelDetail(recommended: recommended))
            } icon: {
                Image(systemName: "info.circle.fill").foregroundStyle(.tint)
            }
        }
    }

    /// Set Up…'s question, with the models read again meanwhile: it says whether the model
    /// server starts first, as it is now.
    private func askToSetUp() {
        confirming = true
        Task { await model.refresh(.models) }
    }

    /// Restart Voice Server: the models read again first, so whether the model server
    /// starts first is decided on what is so now.
    private func restart() {
        reading = true
        Task {
            await model.refresh(.models)
            reading = false
            start(startServer: nil)
        }
    }

    private func start(startServer: Bool?, runtime: String? = nil) {
        let changed = kokoro.flatMap { $0 == voice.here.voice ? nil : $0 }
        model.setUpThisMac(voice: changed, startServer: startServer, runtime: runtime)
    }
}

// MARK: - A server elsewhere

package struct ServerSection: View {
    /// What the section holds when it first appears.
    package struct Start {
        var address: String?
        var keyTyped: Bool
        var clearing: Bool
        var answer: VoiceServerResult?

        package init(address: String? = nil, keyTyped: Bool = false, clearing: Bool = false,
                     answer: VoiceServerResult? = nil) {
            self.address = address
            self.keyTyped = keyTyped
            self.clearing = clearing
            self.answer = answer
        }
    }

    let voice: VoiceStatus

    @Environment(AppModel.self) private var model
    @State private var address: String
    /// The key typed in the sheet, until Check & Use sends it on stdin. Never shown.
    @State private var key: String?
    /// Remove Key was chosen: Check & Use removes the stored key.
    @State private var clearing = false
    @State private var editingKey = false
    @State private var checking = false
    /// The last answer: what was saved, the handshake's problem, the warnings.
    @State private var answer: VoiceServerResult?
    @State private var failure: KeryxError?

    init(voice: VoiceStatus, start: Start) {
        self.voice = voice
        let current = voice.kind == .server ? voice.baseUrl : nil
        _address = State(initialValue: start.address ?? current ?? "")
        _key = State(initialValue: start.keyTyped ? "preview-key" : nil)
        _answer = State(initialValue: start.answer)
        _clearing = State(initialValue: start.clearing)
    }

    package var body: some View {
        let trimmed = address.trimmingCharacters(in: .whitespacesAndNewlines)
        Section(VoiceWording.title(.server)) {
            LabeledContent {
                TextField("Address", text: $address, prompt: Text("http://studio.local:8000/v1"))
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .multilineTextAlignment(.leading)
                    .frame(width: 280)
                    .disabled(checking)
                    .onSubmit { check(keepAnyway: false) }
            } label: {
                Text("Address")
                Text("The voice server's URL")
            }
            LabeledContent {
                HStack(spacing: 8) {
                    Text(VoiceWording.serverKeyState(typed: key != nil, clearing: clearing, stored: keySet))
                        .foregroundStyle(.secondary)
                    if clearing {
                        Button("Keep Key") { clearing = false }
                            .disabled(checking)
                    } else if keySet, key == nil {
                        Button("Remove Key") { clearing = true }
                            .disabled(checking)
                    }
                    Button(keySet || key != nil ? "Change Key…" : "Set Key…") { editingKey = true }
                        .disabled(checking)
                        .accessibilityLabel("Set the Voice Server's API Key")
                }
            } label: {
                Text("API key")
                Text("Only if the server asks for one")
            }
            if voice.kind == .server,
               let state = VoiceWording.serverState(voice.server.reachable, address: voice.baseUrl) {
                LabeledContent("Server") {
                    HStack(spacing: 6) {
                        StatusDot(state.tone)
                        Text(state.text)
                    }
                }
            }
            if let answer {
                ForEach(answer.warnings, id: \.self) { warning in
                    Label {
                        Text(VoiceWording.sentence(Wording.named(warning, titles: Wording.titles(model.config.value))))
                            .foregroundStyle(.secondary)
                    } icon: {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                    }
                    .font(.callout)
                }
                // Kept anyway: saved, and what the handshake said is still worth knowing.
                if let problem = answer.problem, answer.saved != nil {
                    VStack(alignment: .leading, spacing: 4) {
                        Label {
                            Text("Saved without an answer: " + problemText(problem))
                        } icon: {
                            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                        }
                        if let more = details(answer) { DetailsDisclosure(more).padding(.leading, 24) }
                    }
                    .font(.callout)
                }
                if let problem = answer.problem, answer.saved == nil {
                    VStack(alignment: .leading, spacing: 4) {
                        LabeledContent {
                            // Saving one that did not answer is a click, never the default.
                            Button("Use Anyway") { check(keepAnyway: true) }
                                .disabled(checking)
                        } label: {
                            Label {
                                Text("The server didn't answer the handshake")
                                Text(problemText(problem))
                                    .textSelection(.enabled)
                            } icon: {
                                Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
                            }
                        }
                        if let more = details(answer) { DetailsDisclosure(more).padding(.leading, 28) }
                    }
                }
            }
            PrimaryRow(
                inUse: answer?.saved == nil && voice.kind == .server && trimmed == (voice.baseUrl ?? ""),
                inUseTitle: "In Use", working: checking ? "Checking…" : nil,
                failure: failure.map {
                    VoiceWording.errorText($0, operation: "The voice server wasn't checked.",
                                           titles: Wording.titles(model.config.value))
                },
                saved: answer?.saved != nil ? "Saved" : nil
            ) {
                Button(VoiceWording.primary(.server)) { check(keepAnyway: false) }
                    .disabled(checking || trimmed.isEmpty)
            }
        }
        .onChange(of: address) { answer = nil }
        .sheet(isPresented: $editingKey) {
            SecretSheet(key: "VOICE_API_KEY", title: "Voice server API key", replacing: keySet, typed: "",
                        footnote: VoiceWording.keyFootnote, keep: { typed in
                            // Blank is no key: the stored one (if any) stays.
                            let trimmed = typed.trimmingCharacters(in: .whitespacesAndNewlines)
                            key = trimmed.isEmpty ? nil : trimmed
                            if key != nil { clearing = false }
                        })
        }
    }

    private var keySet: Bool {
        model.config.value?.first { $0.key == "VOICE_API_KEY" }?.set ?? false
    }

    /// The CLI's sentence for what went wrong — or, from a CLI that put the server's own
    /// words there, a sentence standing in for them (they go under Show Details).
    private func problemText(_ problem: String) -> String {
        guard AppFacing.isFit(problem) else { return "It didn't open a Realtime session." }
        return VoiceWording.sentence(Wording.named(problem, titles: Wording.titles(model.config.value)))
    }

    /// What the server said, for Show Details: the CLI's `detail`, or an older CLI's
    /// `problem` when that was the server's own words.
    private func details(_ answer: VoiceServerResult) -> String? {
        answer.detail ?? answer.problem.flatMap { AppFacing.isFit($0) ? nil : $0 }
    }


    private func check(keepAnyway: Bool) {
        let url = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !url.isEmpty, !checking else { return }
        checking = true
        failure = nil
        Task {
            let choice = VoiceWording.serverKey(typed: key, clearing: clearing)
            switch await model.useVoiceServer(url: url, key: choice, keepAnyway: keepAnyway) {
            case let .success(result):
                answer = result
                // Sent (or removed): secrets.toml has it now, and nothing need be held here.
                if result.saved != nil {
                    key = nil
                    clearing = false
                }
            case let .failure(error):
                failure = error
            }
            checking = false
        }
    }
}

// MARK: - Shared

/// The row that ends a mode's section: what is happening (or went wrong) on the left, and
/// the button that saves the mode on the right — or "In Use" when it already is.
private struct PrimaryRow<Content: View>: View {
    let inUse: Bool
    let inUseTitle: String
    let working: String?
    let failure: StreamFailure?
    var saved: String?
    @ViewBuilder let button: () -> Content

    var body: some View {
        // In use, with nothing to say: the radio already says so.
        if !(inUse && working == nil && failure == nil && saved == nil) {
            row
        }
    }

    private var row: some View {
        HStack(spacing: 8) {
            if let working {
                ProgressView().controlSize(.small)
                Text(working).foregroundStyle(.secondary)
            } else if let failure {
                VStack(alignment: .leading, spacing: 4) {
                    Text(failure.message)
                        .problemStyle()
                        .textSelection(.enabled)
                        .lineLimit(3)
                    if let detail = failure.detail { DetailsDisclosure(detail) }
                }
            } else if let saved {
                Label(saved, systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if inUse {
                Label(inUseTitle, systemImage: "checkmark")
                    .foregroundStyle(.secondary)
            } else {
                button()
            }
        }
    }
}
