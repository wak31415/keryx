import KeryxKit
import SwiftUI

/// "Use This Model…": asks what the model is for — the agent's work, the voice's words, or
/// both — then chooses it: for the work, `keryx models use` and an offer to start the model
/// server that runs it; for the voice, `keryx voice llm here`, which downloads, starts and
/// checks it on a server of its own (followed on Local Models); for both, the work's choice,
/// with the voice's words then taken from the agent's model (`voice llm shared`).
package struct UseModelSheet: View {
    /// What the model is for.
    package enum Purpose: String, CaseIterable, Identifiable, Sendable {
        case work, voice, both
        package var id: String { rawValue }
    }

    package enum Stage: Equatable {
        /// Asking, with the choice of whether it becomes the default agent.
        case asking
        /// Chosen; what is left is starting its server and restarting Keryx.
        case chosen
    }

    let entry: CatalogModel

    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var stage: Stage
    @State private var purpose: Purpose
    @State private var everyTask = false
    @State private var working = false
    @State private var failure: KeryxError?

    package init(entry: CatalogModel, stage: Stage = .asking, purpose: Purpose = .work) {
        self.entry = entry
        _stage = State(initialValue: stage)
        _purpose = State(initialValue: purpose)
    }

    package var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            switch stage {
            case .asking: asking
            case .chosen: chosen
            }
            if let failure {
                Text(failure.message)
                    .font(.callout)
                    .problemStyle()
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            buttons
        }
        .padding(20)
        .frame(width: 440)
    }

    // MARK: - Asking

    @ViewBuilder private var asking: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Use \(entry.title)?")
                .font(.headline)
            Text(Self.askingDetail(purpose, assistant: assistant))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        Picker("Use it for", selection: $purpose) {
            ForEach(Purpose.allCases) { Text(Self.title($0, assistant: assistant)).tag($0) }
        }
        .pickerStyle(.segmented)
        .disabled(working)
        if purpose != .voice {
            Toggle("Use it for every task unless you name another agent", isOn: $everyTask)
                .disabled(working)
        }
    }

    private var assistant: String? { Wording.assistant(model.status.value) }

    /// "The Work", "Lyra's Voice", "Both".
    static func title(_ purpose: Purpose, assistant: String?) -> String {
        switch purpose {
        case .work: "The Work"
        case .voice: "\(assistant ?? "The Assistant")'s Voice"
        case .both: "Both"
        }
    }

    /// What choosing it does, for each purpose.
    static func askingDetail(_ purpose: Purpose, assistant: String?) -> String {
        let who = assistant ?? "the assistant"
        switch purpose {
        case .work:
            return "Keryx will run it with llama.cpp on this Mac and send tasks to it."
        case .voice:
            return "Keryx will run it on a llama.cpp server of its own, and \(who)'s voice on this Mac will put "
                + "its replies together with it. Small models answer fastest."
        case .both:
            return "Keryx will send tasks to it, and \(who)'s voice on this Mac will share it for its replies."
        }
    }

    // MARK: - Chosen

    @ViewBuilder private var chosen: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Now Using \(entry.title)")
                .font(.headline)
            Text(chosenDetail)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var llm: LLMServer? { model.models.value?.servers.llm }

    /// The model server is already serving it — by what the server says, not the settings.
    private var serving: Bool {
        llm.map { ServedModel.serves($0, entry.name, models: model.models.value?.models ?? []) } ?? false
    }

    private var chosenDetail: String {
        Self.detail(serving: serving, llamaInstalled: llm?.binary != nil, serverRunning: llm?.reachable == true,
                    keryxRestart: model.configNeedsRestart)
    }

    /// What is left once a model is chosen, in one or two sentences: loading it into the
    /// model server — naming the button below that does it — then restarting Keryx itself,
    /// which this sheet does not do.
    static func detail(serving: Bool, llamaInstalled: Bool, serverRunning: Bool, keryxRestart: Bool) -> String {
        let keryx = "restart Keryx from the Overview to start using it"
        guard !serving else {
            return keryxRestart ? "The model server has it loaded. Then \(keryx)." : "It's running and ready."
        }
        let server: String
        if !llamaInstalled {
            server = "Start Model Server installs llama.cpp and starts the model server with it."
        } else {
            server = serverRunning
                ? "Restart Model Server loads it into the model server."
                : "Start Model Server starts the model server with it."
        }
        return keryxRestart ? "\(server) Then \(keryx)." : server
    }

    // MARK: - Buttons

    @ViewBuilder private var buttons: some View {
        HStack {
            if working {
                ProgressView().controlSize(.small)
            }
            Spacer()
            switch stage {
            case .asking:
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Use Model") { use() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(working || (purpose != .work && !model.canChangeVoiceLLM))
            case .chosen:
                if serving {
                    Button("Done") { dismiss() }
                        .keyboardShortcut(.defaultAction)
                } else {
                    Button("Not Now") { dismiss() }
                        .keyboardShortcut(.cancelAction)
                    Button(llm?.reachable == true ? "Restart Model Server" : "Start Model Server") {
                        startServer()
                    }
                    .keyboardShortcut(.defaultAction)
                    .disabled(working || !model.canStartModelServer)
                }
            }
        }
    }

    /// What choosing it does, for `purpose`: `models use` (the work's), and the voice's words
    /// changed (`voice llm here` for its own, `voice llm shared` for both — unless they are
    /// the agent's already).
    static func plan(_ purpose: Purpose, model name: String, voice: VoiceStatus?)
        -> (use: Bool, voice: VoiceLLMChoice?) {
        switch purpose {
        case .work: (true, nil)
        case .voice: (false, .here(model: name))
        case .both: (true, voice?.llmSource == .shared ? nil : .shared)
        }
    }

    private func use() {
        let plan = Self.plan(purpose, model: entry.name, voice: model.voice.value)
        guard plan.use else {
            // Followed on Local Models' Voice LLM Server, where its steps show.
            if let choice = plan.voice, model.chooseVoiceLLM(choice) {
                model.show(inModels: ModelsAnchor.voiceLLMServer)
                dismiss()
            } else {
                failure = .refused(Self.voiceBusy)
            }
            return
        }
        working = true
        failure = nil
        Task {
            failure = await model.use(model: entry.name, defaultAgent: everyTask)
            working = false
            guard failure == nil else { return }
            stage = .chosen
            if let choice = plan.voice {
                // Its steps, and any refusal, show on Voice LLM Server.
                if model.chooseVoiceLLM(choice) {
                    model.show(inModels: ModelsAnchor.voiceLLMServer)
                } else {
                    failure = .refused(Self.voiceNotMoved)
                }
            }
        }
    }

    static let voiceBusy = "The voice's words are already being changed, or its voice server set up. Try again once that has finished."
    static let voiceNotMoved = "The work now uses it, but the voice's words were not moved to it: they are being changed "
        + "already. Choose Same as Local Agent in Voice once that has finished."

    /// Starts it, and leaves its steps to Local Models' Model Server, scrolled to.
    private func startServer() {
        model.runModelServer()
        model.show(inModels: ModelsAnchor.modelServer)
        dismiss()
    }
}
