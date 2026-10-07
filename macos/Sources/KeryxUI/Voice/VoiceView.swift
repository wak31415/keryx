import KeryxKit
import SwiftUI

/// The Voice screen: how the assistant speaks — OpenAI Realtime, a voice server on this Mac,
/// or one elsewhere — and everything each needs. Picking a way shows its section; nothing
/// is saved until that section's button (Use OpenAI Realtime, Set Up…, Check & Use). Once
/// one is, the banner at the top offers the restart that loads it.
public struct VoiceView: View {
    @Environment(AppModel.self) private var model
    /// The way picked on screen; `nil` until the owner picks one, which shows the one in use.
    @State private var picked: VoiceMode?
    private let server: ServerSection.Start

    public init() {
        self.init(mode: nil)
    }

    /// `mode`: the way picked when the screen appears; `server`: the server section's state
    /// then (the catalog's).
    package init(mode: VoiceMode?, server: ServerSection.Start = .init()) {
        _picked = State(initialValue: mode)
        self.server = server
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
                case .here: HereSection(voice: voice)
                case .server: ServerSection(voice: voice, start: server)
                }
            }
            .formStyle(.grouped)
        }
        .task {
            await model.refresh(.voice)
            // The words model's title and the API key rows read these.
            if model.models.value == nil { await model.refresh(.models) }
            if model.config.value == nil { await model.refresh(.config) }
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
                    Button(voice.openaiKeySet ? "Change…" : "Set…") { editingKey = true }
                        .accessibilityLabel(voice.openaiKeySet ? "Change OpenAI API Key" : "Set OpenAI API Key")
                }
            } label: {
                Text("API key")
                Text("Kept in secrets.toml")
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
                failure: failure?.message
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

    @Environment(AppModel.self) private var model
    /// The Kokoro voice picked here, applied by the next setup; `nil`: the one set.
    @State private var kokoro: String?
    @State private var confirming = false

    var body: some View {
        let here = voice.here
        let setup = model.voiceSetup
        let inUse = voice.kind == .here
        Section(VoiceWording.title(.here)) {
            if voice.needsLocalModel {
                LabeledContent {
                    Button("Go to Local Models") { model.selection = .models }
                } label: {
                    Label {
                        Text(VoiceWording.needsModelTitle)
                        Text(VoiceWording.needsModelDetail)
                    } icon: {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                    }
                }
            }
            LabeledContent("Voice server") {
                HStack(spacing: 6) {
                    StatusDot(here.reachable ? .ok : .inactive)
                    Text(VoiceWording.hereState(here))
                        .textSelection(.enabled)
                }
                .help(here.program ?? "")
            }
            LabeledContent("Starts at login") {
                Text(here.unitInstalled ? "Yes" : "No")
                    .foregroundStyle(.secondary)
            }
            LabeledContent {
                Text(VoiceWording.wordsModel(here.wordsModel, models: model.models.value?.models))
                    .foregroundStyle(here.wordsModel == nil ? .secondary : .primary)
                    .textSelection(.enabled)
            } label: {
                Text("Words")
                Text("The local model that puts the replies together")
            }
            LabeledContent {
                OptionMenu(key: "VOICE_SERVER_VOICE", title: "Kokoro voice", current: kokoro ?? here.voice) {
                    kokoro = $0
                }
                .disabled(setup.isBusy)
            } label: {
                Text("Kokoro voice")
                if let kokoro, kokoro != here.voice {
                    Text("Takes effect when you choose \(inUse ? "Restart Voice Server" : "Set Up")")
                } else if let detail = OptionWording.detail(of: kokoro ?? here.voice,
                                                            in: model.options["VOICE_SERVER_VOICE"]?.value) {
                    Text(detail)
                }
            }
            if let phase = setup.phase {
                SetupProgress(phase: phase, cancel: { setup.cancel() }, dismiss: { setup.dismiss() })
            }
            PrimaryRow(inUse: false, inUseTitle: "", working: nil, failure: nil) {
                Button(inUse ? "Restart Voice Server" : "Set Up…") {
                    if inUse { start() } else { confirming = true }
                }
                .disabled(setup.isBusy || voice.needsLocalModel)
                .help(voice.needsLocalModel ? VoiceWording.needsModelTitle : "")
            }
        }
        .confirmationDialog(VoiceWording.setUpQuestion, isPresented: $confirming) {
            Button("Set Up") { start() }
        } message: {
            Text(VoiceWording.setUpMessage)
        }
    }

    private func start() {
        let changed = kokoro.flatMap { $0 == voice.here.voice ? nil : $0 }
        model.setUpVoiceHere(voice: changed)
        kokoro = nil
    }
}

/// A setup under way, stopping, failed or done: its step, a bar, and Cancel.
private struct SetupProgress: View {
    let phase: VoiceSetup.Phase
    let cancel: () -> Void
    let dismiss: () -> Void

    var body: some View {
        switch phase {
        case let .running(step, message, progress):
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline) {
                    Text(VoiceWording.running(message))
                    Spacer()
                    if let progress, progress.total > 0 {
                        Text(VoiceWording.sizes(progress))
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                    } else if let count = VoiceWording.stepCount(step) {
                        Text(count)
                            .foregroundStyle(.secondary)
                    }
                }
                HStack(spacing: 10) {
                    if let progress, progress.total > 0 {
                        ProgressView(value: Double(min(progress.done, progress.total)), total: Double(progress.total))
                    } else {
                        ProgressView()
                            .progressViewStyle(.linear)
                    }
                    Button("Cancel", action: cancel)
                        .controlSize(.small)
                }
            }
            .padding(.vertical, 2)
        case .cancelling:
            VStack(alignment: .leading, spacing: 6) {
                Text("Stopping…")
                    .foregroundStyle(.secondary)
                HStack(spacing: 10) {
                    ProgressView()
                        .progressViewStyle(.linear)
                    Button("Cancel") {}
                        .controlSize(.small)
                        .disabled(true)
                }
            }
            .padding(.vertical, 2)
        case let .failed(message):
            LabeledContent {
                Button("Dismiss", action: dismiss)
            } label: {
                Label {
                    Text("Setup didn't finish")
                    Text(VoiceWording.sentence(message))
                        .foregroundStyle(.red)
                        .textSelection(.enabled)
                } icon: {
                    Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
                }
            }
        case let .finished(url):
            Label {
                Text("Set up at \(VoiceWording.address(url))")
            } icon: {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            }
        }
    }
}

// MARK: - A server elsewhere

package struct ServerSection: View {
    /// What the section holds when it first appears.
    package struct Start {
        var address: String?
        var keyTyped: Bool
        var answer: VoiceServerResult?

        package init(address: String? = nil, keyTyped: Bool = false, answer: VoiceServerResult? = nil) {
            self.address = address
            self.keyTyped = keyTyped
            self.answer = answer
        }
    }

    let voice: VoiceStatus

    @Environment(AppModel.self) private var model
    @State private var address: String
    /// The key typed in the sheet, until Check & Use sends it on stdin. Never shown.
    @State private var key: String?
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
                    Text(keyState)
                        .foregroundStyle(.secondary)
                    Button("Key…") { editingKey = true }
                        .disabled(checking)
                        .accessibilityLabel("Set the Voice Server's API Key")
                }
            } label: {
                Text("API key")
                Text("Only if the server asks for one")
            }
            if voice.kind == .server, let reachable = voice.server.reachable {
                LabeledContent("Server") {
                    HStack(spacing: 6) {
                        StatusDot(reachable ? .ok : .error)
                        Text(reachable ? "Answering" : "Not answering")
                    }
                }
            }
            if let answer {
                ForEach(answer.warnings, id: \.self) { warning in
                    Label {
                        Text(VoiceWording.sentence(warning))
                            .foregroundStyle(.orange)
                    } icon: {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                    }
                    .font(.callout)
                }
                if let problem = answer.problem, answer.saved == nil {
                    LabeledContent {
                        Button("Use Anyway") { check(keepAnyway: true) }
                            .disabled(checking)
                    } label: {
                        Label {
                            Text("The server didn't answer the handshake")
                            Text(VoiceWording.sentence(problem))
                                .foregroundStyle(.red)
                                .textSelection(.enabled)
                        } icon: {
                            Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
                        }
                    }
                }
            }
            PrimaryRow(
                inUse: answer?.saved == nil && voice.kind == .server && trimmed == (voice.baseUrl ?? ""),
                inUseTitle: "In Use", working: checking ? "Checking…" : nil, failure: failure?.message,
                saved: answer?.saved != nil ? "Saved" : nil
            ) {
                Button(VoiceWording.primary(.server)) { check(keepAnyway: false) }
                    .disabled(checking || trimmed.isEmpty)
            }
        }
        .onChange(of: address) { answer = nil }
        .sheet(isPresented: $editingKey) {
            SecretSheet(key: "VOICE_API_KEY", title: "Voice server API key", replacing: keySet, typed: "",
                        footnote: VoiceWording.keyFootnote, keep: { key = $0 })
        }
    }

    private var keySet: Bool {
        model.config.value?.first { $0.key == "VOICE_API_KEY" }?.set ?? false
    }

    private var keyState: String {
        if key != nil { return "Sent with Check & Use" }
        return keySet ? "Set" : "Not set"
    }

    private func check(keepAnyway: Bool) {
        let url = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !url.isEmpty, !checking else { return }
        checking = true
        failure = nil
        Task {
            switch await model.useVoiceServer(url: url, key: key, keepAnyway: keepAnyway) {
            case let .success(result):
                answer = result
                // Sent: it is in secrets.toml now, and need not be held here.
                if result.saved != nil { key = nil }
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
    let failure: String?
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
                Text(failure)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
                    .lineLimit(3)
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
