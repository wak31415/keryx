import AppKit
import KeryxKit
import SwiftUI

/// The Local Models screen: what this Mac can run, the model the local agent uses and its
/// server, every model in the catalog to download or choose, and the voice server.
public struct ModelsView: View {
    @Environment(AppModel.self) private var model
    @State private var choosing: CatalogModel?

    public init() {}

    public var body: some View {
        StateView(model.models, section: "Local Models", retry: { Task { await model.refresh(.models) } }) { report in
            Form {
                ThisMacSection(report: report)
                LocalAgentSection(report: report)
                CatalogSection(report: report) { choosing = $0 }
                VoiceSection(voice: report.servers.voice)
            }
            .formStyle(.grouped)
        }
        // Read here: the status poll does not read the models.
        .task { await model.refresh(.models) }
        .sheet(item: $choosing) { UseModelSheet(entry: $0) }
    }
}

// MARK: - This Mac

private struct ThisMacSection: View {
    let report: ModelsReport

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
    @State private var starting = false
    @State private var failure: KeryxError?

    var body: some View {
        let agent = report.servers.localAgent
        let llm = report.servers.llm
        Section("Local Agent") {
            if let name = agent.model {
                LabeledContent("Model") {
                    Text(title(of: name))
                        .textSelection(.enabled)
                }
                LabeledContent("Model server") {
                    VStack(alignment: .trailing, spacing: 2) {
                        HStack(spacing: 6) {
                            StatusDot(llm.reachable ? .ok : .inactive)
                            Text(llm.reachable ? "Running at \(Address.of(llm.url, port: llm.port))" : "Not running")
                                .textSelection(.enabled)
                        }
                        if let served = ServedModel.stale(llm, configured: name, models: report.models) {
                            Text("Serving \(title(of: served)) — restart the model server to load \(title(of: name))")
                                .font(.callout)
                                .foregroundStyle(.orange)
                                .multilineTextAlignment(.trailing)
                        }
                    }
                }
                LabeledContent("Used for") {
                    Text(Self.usedFor(agent))
                }
            } else {
                Text("No local model yet — download one below and choose Use This Model.")
                    .foregroundStyle(.secondary)
            }

            if llm.binary == nil {
                LabeledContent {
                    OpenSetupButton()
                } label: {
                    Label {
                        Text("llama.cpp isn't installed")
                    } icon: {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                    }
                }
            } else if agent.model != nil {
                serverControl(llm)
            }

            if let failure {
                Text(failure.message)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
        }
    }

    /// Start (or restart) the model server — or, with no checkout to install it from, the
    /// way to do it in Terminal.
    @ViewBuilder private func serverControl(_ llm: LLMServer) -> some View {
        if model.location.value?.checkout == nil {
            LabeledContent {
                OpenSetupButton()
            } label: {
                Text("Start it with keryx setup")
                    .foregroundStyle(.secondary)
            }
        } else {
            HStack {
                if starting {
                    ProgressView().controlSize(.small)
                    Text(llm.unitInstalled ? "Restarting…" : "Starting…")
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button(llm.unitInstalled ? "Restart Model Server" : "Start Model Server") {
                    starting = true
                    failure = nil
                    Task {
                        failure = await model.startModelServer()
                        starting = false
                    }
                }
                .disabled(starting)
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

// MARK: - Catalog

private struct CatalogSection: View {
    let report: ModelsReport
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

// MARK: - Voice

private struct VoiceSection: View {
    let voice: VoiceServer

    var body: some View {
        Section("Voice") {
            LabeledContent("Voice server") {
                if let url = voice.url {
                    HStack(spacing: 6) {
                        StatusDot(voice.reachable ? .ok : .inactive)
                        Text(voice.reachable ? "Running at \(Address.of(url, port: voice.port))" : "Not running")
                            .textSelection(.enabled)
                    }
                } else {
                    Text("OpenAI Realtime (not local)")
                        .foregroundStyle(.secondary)
                }
            }
            if voice.url == nil {
                LabeledContent {
                    OpenSetupButton()
                } label: {
                    Text("Set up a local voice with keryx setup")
                        .foregroundStyle(.secondary)
                }
            }
        }
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

/// "Open Setup in Terminal": `keryx setup` in a Terminal window, where what it needs (a
/// download, a sign-in) can ask.
struct OpenSetupButton: View {
    @Environment(AppModel.self) private var model
    @State private var failure: KeryxError?

    var body: some View {
        Button("Open Setup in Terminal") {
            guard let location = model.location.value else { return }
            do {
                try TerminalLauncher.open(location, arguments: ["setup"])
            } catch {
                failure = (error as? KeryxError) ?? .terminal(error.localizedDescription)
            }
        }
        .disabled(model.location.value == nil)
        .alert(failure?.title ?? "", isPresented: Binding(
            get: { failure != nil }, set: { if !$0 { failure = nil } }
        )) {
            Button("OK") {}
        } message: {
            Text(failure?.message ?? "")
        }
    }
}
