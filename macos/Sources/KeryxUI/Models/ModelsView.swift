import AppKit
import KeryxKit
import SwiftUI

/// The Local Models screen: what this Mac can run and how its memory is shared, the model
/// the local agent uses (chosen on Agents), the two model servers — the agent's (`keryx
/// models start`) and the voice LLM's own (`--voice-llm`) — the voice's speech models, every
/// model in the catalog to download or use, and under Advanced the servers' ports.
public struct ModelsView: View {
    @Environment(AppModel.self) private var model
    @State private var choosing: CatalogModel?
    /// What was asked to be shown (`AppModel.modelsSpotlight`): a model is marked a moment,
    /// a section only scrolled to.
    @State private var marked: String?
    /// Whether the Advanced disclosure (the model servers' ports) starts open.
    private let advancedOpen: Bool

    public init() {
        self.init(advancedOpen: false)
    }

    package init(advancedOpen: Bool) {
        self.advancedOpen = advancedOpen
    }

    /// This screen answers `request` (`RequestAnswer`): shown at what is left to do here.
    static func answers(_ request: AppRequest) -> Bool {
        RequestAnswer.of(request) == .models
    }

    public var body: some View {
        StateView(model.models, section: "Local Models", retry: { Task { await model.refresh(.models) } }) { report in
            ScrollViewReader { proxy in
                Form {
                    ThisMacSection(report: report)
                    LocalAgentSection(report: report)
                    ModelServerSection(report: report)
                    VoiceLLMServerSection(report: report)
                    VoiceModelsSection(report: report)
                    CatalogSection(report: report, marked: marked) { choosing = $0 }
                    ScreenAdvancedSection(screen: .models, open: advancedOpen)
                        .id(ModelsAnchor.advanced)
                }
                .formStyle(.grouped)
                .onChange(of: model.modelsSpotlight, initial: true) { _, anchor in
                    guard let anchor else { return }
                    spotlight(anchor, proxy: proxy)
                }
                // The Setup Assistant's "Choose a Model" or "Open Local Models": shown at
                // what is left to do here.
                .onChange(of: model.requested, initial: true) { _, requested in
                    guard let requested, Self.answers(requested) else { return }
                    model.didHandle(requested)
                    if let anchor = ModelsWording.nextStep(report) { spotlight(anchor, proxy: proxy) }
                }
            }
        }
        // Read here: the status poll does not read the models, nor the voice's.
        .task {
            await model.refresh(.models)
            if model.voice.value == nil { await model.refresh(.voice) }
            // The voice LLM's own unit.
            if model.service.value == nil { await model.refresh(.service) }
            // Advanced's ports.
            if model.config.value == nil { await model.refresh(.config) }
        }
        .sheet(item: $choosing) { UseModelSheet(entry: $0) }
    }

    /// Scrolls to `anchor` and marks it for a few seconds.
    private func spotlight(_ anchor: String, proxy: ScrollViewProxy) {
        model.modelsSpotlight = nil
        marked = anchor
        Task {
            // Once the rows are laid out.
            await Task.yield()
            withAnimation { proxy.scrollTo(anchor, anchor: .center) }
            try? await Task.sleep(for: .seconds(3))
            if marked == anchor { withAnimation(.easeOut(duration: 0.6)) { marked = nil } }
        }
    }
}

/// A catalog row asked to be shown: tinted a moment.
private struct Marked: ViewModifier {
    let on: Bool

    func body(content: Content) -> some View {
        content.background {
            RoundedRectangle(cornerRadius: 6)
                .fill(Color.accentColor.opacity(on ? 0.14 : 0))
                .padding(.horizontal, -6)
                .padding(.vertical, -2)
        }
    }
}

// MARK: - This Mac

private struct ThisMacSection: View {
    let report: ModelsReport

    @Environment(AppModel.self) private var model

    var body: some View {
        let hardware = report.hardware
        Section("This Mac") {
            LabeledContent {
                Text("\(Formatting.gigabytes(hardware.modelMemoryGb)) of \(Formatting.gigabytes(Self.total(hardware)))")
                    .textSelection(.enabled)
            } label: {
                Text("Memory for models")
                Text(Self.runsOn(hardware))
            }
            MemoryBar(shares: MemoryShares.of(report, voice: model.voice.value), available: hardware.modelMemoryGb)
            if let free = Self.freeBytes(near: report.modelsDir) {
                LabeledContent("Free disk") {
                    Text(Formatting.bytes(free))
                        .textSelection(.enabled)
                }
            }
            LabeledContent("Models folder") {
                HStack(spacing: 10) {
                    Text((report.modelsDir as NSString).abbreviatingWithTildeInPath)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .textSelection(.enabled)
                        .help(report.modelsDir)
                    Button("Reveal in Finder") { Self.reveal(report.modelsDir) }
                }
            }
        }
    }

    /// The memory the models share: the card's own when there is one, else the Mac's.
    static func total(_ hardware: Hardware) -> Double {
        hardware.vramGb > 0 ? hardware.vramGb : hardware.ramGb
    }

    /// "Apple silicon · Metal", "CPU only".
    static func runsOn(_ hardware: Hardware) -> String {
        switch hardware.accelerator {
        case "cpu": return "CPU only"
        case let accelerator:
            let name = ["metal": "Metal", "cuda": "CUDA", "rocm": "ROCm", "vulkan": "Vulkan"][accelerator]
                ?? accelerator.uppercased()
            return [hardware.gpu, name].compactMap { $0 }.joined(separator: " · ")
        }
    }

    /// Free space on the volume the models folder is on — asked of the nearest folder that
    /// exists, since the models folder itself appears only with the first download.
    static func freeBytes(near path: String) -> Int64? {
        let values = try? existing(path).resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return values?.volumeAvailableCapacityForImportantUsage
    }

    /// The folder in Finder, or the nearest one that exists.
    static func reveal(_ path: String) {
        NSWorkspace.shared.activateFileViewerSelecting([existing(path)])
    }

    /// `path`, or the nearest folder above it that exists.
    private static func existing(_ path: String) -> URL {
        var url = URL(fileURLWithPath: path)
        while !FileManager.default.fileExists(atPath: url.path), url.pathComponents.count > 1 {
            url.deleteLastPathComponent()
        }
        return url
    }
}

// MARK: - Local agent

private struct LocalAgentSection: View {
    let report: ModelsReport

    @Environment(AppModel.self) private var model

    var body: some View {
        let agent = report.servers.localAgent
        Section("Local Agent") {
            // Read here; the one editor is Configuration's Agents tab.
            LabeledContent {
                Button(ModelsWording.changeOnAgents) { model.request(.setting(key: "LOCAL_AGENT_MODEL")) }
                    .buttonStyle(.link)
            } label: {
                if let name = agent.model {
                    Text(title(of: name))
                        .textSelection(.enabled)
                    Text(Self.usedFor(agent))
                } else {
                    Text("No local model yet")
                    Text("Download one below and choose Use This Model.")
                }
            }
        }
    }

    private func title(of name: String) -> String {
        report.models.first { $0.name == name }?.title ?? name
    }

    static func usedFor(_ agent: LocalAgent) -> String {
        if agent.default { return "Every task, unless you name another agent" }
        return agent.enabled ? "Only when you ask for it by name" : "Nothing yet — it isn't turned on"
    }
}

// MARK: - Model server

/// The llama.cpp server that runs the chosen model: whether it answers, and Start (or
/// Restart) and Stop. Start is `keryx models start`, step by step — llama.cpp installed
/// when it is missing, its service, the model loaded, the model it serves checked — with
/// Cancel; a server that doesn't come up has its service removed by the CLI.
private struct ModelServerSection: View {
    let report: ModelsReport

    @Environment(AppModel.self) private var model
    @State private var stopping = false
    @State private var failure: KeryxError?
    @State private var confirmingStop = false

    var body: some View {
        let llm = report.servers.llm
        let run = model.modelServerRun
        let configured = report.servers.localAgent.model ?? llm.model
        Section {
            LabeledContent {
                VStack(alignment: .trailing, spacing: 2) {
                    HStack(spacing: 6) {
                        StatusDot(llm.reachable ? .ok : .inactive)
                        Text(ModelsWording.serverState(llm))
                            .textSelection(.enabled)
                    }
                    if let configured, let served = ServedModel.stale(llm, configured: configured, models: report.models) {
                        Text("Serving \(title(of: served)) — restart the model server to load \(title(of: configured))")
                            .font(.callout)
                            .problemStyle(.warning)
                            .multilineTextAlignment(.trailing)
                    }
                }
            } label: {
                Text("Status")
                // What runs it, and whether it starts at login, under the one status line; the
                // program's path is behind Reveal in Finder.
                Text(ModelsWording.serverCaption(llm))
            }
            progress(run)
            // While a start runs or has failed, its own rows are the ones to act on.
            if run.phase == nil || run.result != nil {
                controls(report.servers)
            }
        } header: {
            Text("Agent Model Server")
        }
        .id(ModelsAnchor.modelServer)
        .confirmationDialog(stopQuestion.title, isPresented: $confirmingStop) {
            Button("Stop Model Server", role: .destructive, action: stop)
        } message: {
            Text(stopQuestion.message)
        }
    }

    /// A start under way, or one that went wrong; one that worked says so in Status.
    @ViewBuilder private func progress(_ run: StreamRun<ModelServerStarted>) -> some View {
        if case let .failed(failure)? = run.phase, LocalRefusal.of(failure.message) == .needsModel {
            RunGuidanceRow(title: ModelsWording.chooseModelFirst, message: ModelsWording.chooseModelDetail) {
                Button("Dismiss") { run.dismiss() }
            }
        } else if let display = RunDisplay.of(run), case .finished = display {
            EmptyView()
        } else if let display = RunDisplay.of(run) {
            StreamRunView(groups: [RunStepGroup(steps: RunSteps.steps(RunSteps.modelServer, reached: run.lastStep,
                                                                      stage: RunSteps.stage(run.phase)))],
                          display: display, cancel: { model.cancelModelServer() },
                          failureTitle: ModelsWording.serverFailed, paths: model.status.value?.paths) {
                Button("Dismiss") { run.dismiss() }
                Button("Try Again") { model.runModelServer() }
            }
        }
    }

    /// Start or Restart, and Stop; or why it can't be started from here.
    @ViewBuilder private func controls(_ servers: Servers) -> some View {
        let llm = servers.llm
        let cannot = ModelsWording.cannotStart(servers)
        HStack(spacing: 8) {
            if let binary = llm.binary, !stopping {
                Button("Reveal in Finder") { ThisMacSection.reveal(binary) }
                    .help("Shows llama.cpp's server program in the Finder")
            }
            if stopping {
                ProgressView().controlSize(.small)
                Text("Stopping…").foregroundStyle(.secondary)
            } else if let failure {
                let said = VoiceWording.errorText(failure, operation: "The model server didn't stop.",
                                                  titles: Wording.titles(model.config.value))
                VStack(alignment: .leading, spacing: 4) {
                    Text(said.message)
                        .problemStyle()
                        .textSelection(.enabled)
                        .lineLimit(3)
                    if let detail = said.detail { DetailsDisclosure(detail) }
                }
            } else if let cannot {
                Text(cannot).foregroundStyle(.secondary)
            }
            Spacer()
            if llm.unitInstalled || llm.reachable {
                Button("Stop Model Server") {
                    // Only while the voice on this Mac takes its words from it.
                    if model.voice.value?.kind == .here, model.voiceWordsAreTheAgents { confirmingStop = true } else { stop() }
                }
                .disabled(stopping || model.stoppingModelServer || model.modelServerRun.isBusy || model.voiceSetup.isBusy)
                .help(model.voiceSetup.isBusy ? "Wait for the voice setup to finish" : "")
            }
            if cannot == nil {
                Button(llm.reachable ? "Restart Model Server" : "Start Model Server") {
                    failure = nil
                    model.runModelServer()
                }
                .disabled(stopping || !model.canStartModelServer)
                .help(model.voiceSetup.isBusy ? "Wait for the voice setup to finish" : "")
            }
        }
    }

    private var stopQuestion: (title: String, message: String) {
        ModelsWording.stopQuestion(assistant: Wording.assistant(model.status.value))
    }

    private func stop() {
        stopping = true
        failure = nil
        Task {
            if case let .failure(error) = await model.stopModelServer() { failure = error }
            stopping = false
        }
    }

    private func title(of name: String) -> String {
        report.models.first { $0.name == name }?.title ?? name
    }
}

// MARK: - Voice models

/// The models the voice on this Mac hears and speaks with (`VOICE_SERVER_STT`, `_TTS`),
/// whether they are on this Mac yet, and Download (`keryx voice download`) to fetch them
/// ahead of the voice server's first start.
private struct VoiceModelsSection: View {
    let report: ModelsReport

    @Environment(AppModel.self) private var model

    var body: some View {
        Section {
            if let voice = model.voice.value {
                rows(voice)
            } else if let error = model.voice.error {
                Text(error.message)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            } else {
                HStack {
                    ProgressView().controlSize(.small)
                    Text("Reading the voice…").foregroundStyle(.secondary)
                }
            }
        } header: {
            Text("Voice Models")
        } footer: {
            Text("What the voice on this Mac hears and speaks with, chosen in Voice.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .id(ModelsAnchor.voiceModels)
        // The speech models' names, as their lists say them.
        .task {
            await model.loadOptions("VOICE_SERVER_STT")
            await model.loadOptions("VOICE_SERVER_TTS")
        }
    }

    @ViewBuilder private func rows(_ voice: VoiceStatus) -> some View {
        let here = voice.here
        let run = model.voiceDownloadRun
        if let display = RunDisplay.of(run), run.result == nil {
            StreamRunView(groups: [RunStepGroup(steps: RunSteps.steps(RunSteps.voiceDownload, reached: run.lastStep,
                                                                      stage: RunSteps.stage(run.phase)))],
                          display: display, cancel: { run.cancel() }, failureTitle: ModelsWording.downloadFailed,
                          paths: model.status.value?.paths) {
                Button("Dismiss") { run.dismiss() }
                Button("Try Again") { model.downloadVoiceModels() }
            }
        } else {
            // Not beside a voice setup, which fetches them too — nor one waiting on its model server.
            downloadRow(here, settingUp: !model.canDownloadVoiceModels)
        }
        voiceServerRow(voice)
    }

    private func downloadRow(_ here: HereVoice, settingUp: Bool) -> some View {
        let ready = here.modelsReady
        let names = VoiceWording.speechModels(here, stt: model.options["VOICE_SERVER_STT"]?.value,
                                              tts: model.options["VOICE_SERVER_TTS"]?.value).names
        return LabeledContent {
            if ready {
                Label(ModelsWording.speechModels(ready: true), systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.secondary)
            } else {
                HStack(spacing: 8) {
                    Text(ModelsWording.speechModels(ready: false))
                        .foregroundStyle(.secondary)
                    Button("Download") { model.downloadVoiceModels() }
                        .disabled(settingUp)
                        .help(settingUp ? "The voice setup is downloading them" : "")
                }
            }
        } label: {
            Text("Speech models")
            Text(ready ? names : names + ". " + ModelsWording.speechModelsNote)
        }
    }

    private func voiceServerRow(_ voice: VoiceStatus) -> some View {
        let server = report.servers.voice
        return LabeledContent {
            if voice.kind == .here || server.unitInstalled {
                HStack(spacing: 6) {
                    StatusDot(server.reachable ? .ok : .inactive)
                    Text(server.reachable ? "Running at \(Address.of(server.url, port: server.port))" : "Not running")
                        .textSelection(.enabled)
                }
            } else {
                Button("Open Voice") { model.selection = .voice }
            }
        } label: {
            Text("Voice server")
            if voice.kind != .here, !server.unitInstalled {
                Text("Not set up on this Mac: Set Up in Voice does it")
            }
        }
    }
}

// MARK: - Catalog

private struct CatalogSection: View {
    let report: ModelsReport
    /// The model asked to be shown, marked a moment.
    let marked: String?
    let choose: (CatalogModel) -> Void

    @Environment(AppModel.self) private var model

    var body: some View {
        let downloads = model.downloads
        Section {
            ForEach(ModelsOrder.sorted(report.models)) { entry in
                ModelRow(
                    entry: entry,
                    action: ModelRow.action(for: entry, phase: downloads.active[entry.name],
                                            inUse: report.servers.localAgent.model == entry.name),
                    available: report.hardware.modelMemoryGb,
                    busy: downloads.isBusy,
                    download: { model.download(entry.name) },
                    cancel: { downloads.cancel(entry.name) },
                    use: { choose(entry) })
                .id(entry.name)
                .modifier(Marked(on: marked == entry.name))
            }
        } header: {
            Text("Models")
        }
    }
}

/// What the model server is serving, against what the local agent is set to use.
enum ServedModel {
    /// Whether the running server is serving `name` — by the name it was given, or by the
    /// catalog title an older server reported.
    static func serves(_ llm: LLMServer, _ name: String, models: [CatalogModel]) -> Bool {
        guard llm.reachable, let served = llm.servedModel else { return false }
        return served == name || served == models.first { $0.name == name }?.title
    }

    /// The model the running server says it serves, when that is not `configured` — the
    /// restart that loads `configured` is still to come. `nil` when it is, or when the
    /// server is not running or did not say.
    static func stale(_ llm: LLMServer, configured: String, models: [CatalogModel]) -> String? {
        guard llm.reachable, let served = llm.servedModel, !serves(llm, configured, models: models) else { return nil }
        return served
    }
}

/// The catalog's order on screen: the recommended model first, then those that fit this
/// Mac, then the rest — each group in the catalog's own order.
enum ModelsOrder {
    static func sorted(_ models: [CatalogModel]) -> [CatalogModel] {
        models.enumerated().sorted { a, b in
            let (ra, rb) = (rank(a.element), rank(b.element))
            return ra != rb ? ra < rb : a.offset < b.offset
        }.map(\.element)
    }

    private static func rank(_ entry: CatalogModel) -> Int {
        entry.recommended ? 0 : entry.fits ? 1 : 2
    }
}

// MARK: - Shared

/// "127.0.0.1:8090" from a server's URL, else the loopback address and its port.
enum Address {
    static func of(_ url: String?, port: Int) -> String {
        if let url, let parts = URLComponents(string: url), let host = parts.host {
            return "\(host):\(parts.port ?? port)"
        }
        return "127.0.0.1:\(port)"
    }
}
