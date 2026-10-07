import AppKit
import KeryxKit
import SwiftUI

/// The Plugins section: the four optional voice tools, each on or off with its settings,
/// and under them the owner's own tools in the tools folder, with any the next call would
/// refuse and why. Nothing here needs a restart: tools load with each call.
public struct PluginsView: View {
    @Environment(AppModel.self) private var model
    /// Plugins with a change under way, by name.
    @State private var working: Set<String> = []
    /// What went wrong on each card, by name, said under it.
    @State private var failures: [String: String]
    /// The settings of plugins that are off, until they are turned on.
    @State private var drafts: [String: PluginDraft] = [:]
    @State private var turningOff: Plugin?

    public init() {
        self.init(failures: [:])
    }

    /// `failures`: what each card says went wrong when the screen appears (the catalog's).
    package init(failures: [String: String]) {
        _failures = State(initialValue: failures)
    }

    public var body: some View {
        StateView(model.plugins, section: "Plugins", retry: { Task { await model.refresh(.plugins) } }) { report in
            Form {
                ForEach(report.plugins) { plugin in
                    PluginCard(
                        plugin: plugin,
                        working: working.contains(plugin.name),
                        failure: failures[plugin.name],
                        draft: Binding(get: { drafts[plugin.name] ?? PluginDraft() },
                                       set: { drafts[plugin.name] = $0 }),
                        turnOn: { turnOn(plugin) },
                        askOff: { turningOff = plugin },
                        save: { values in save(plugin, values) })
                }
                OwnToolsSection(directory: report.directory)
            }
            .formStyle(.grouped)
        }
        .task {
            await model.refresh(.plugins, .tools, .integrations)
            if model.sshHosts.value == nil { await model.refresh(.sshHosts) }
        }
        .confirmationDialog(question?.title ?? "", isPresented: Binding(
            get: { turningOff != nil }, set: { if !$0 { turningOff = nil } }
        ), presenting: turningOff) { plugin in
            Button(question?.button ?? "Turn Off", role: .destructive) { turnOff(plugin) }
            Button("Cancel", role: .cancel) {}
        } message: { _ in
            Text(question?.message ?? "")
        }
    }

    private var question: IntegrationQuestion? {
        turningOff.map { PluginsWording.turnOffQuestion($0, assistant: Wording.assistant(model.status.value)) }
    }

    // MARK: - Actions

    /// Installs it with what its draft holds; the draft goes once it is on.
    private func turnOn(_ plugin: Plugin) {
        let values = drafts[plugin.name]?.values ?? PluginValues()
        perform(plugin) {
            let result = await model.installPlugin(plugin.name, values: values)
            if case .success = result { drafts[plugin.name] = nil }
            return result
        }
    }

    private func turnOff(_ plugin: Plugin) {
        perform(plugin) { await model.removePlugin(plugin.name) }
    }

    private func save(_ plugin: Plugin, _ values: PluginValues) {
        perform(plugin) { await model.setPlugin(plugin.name, values: values) }
    }

    /// Runs `work` for the card, with its spinner up; a failure is said under it.
    private func perform(_ plugin: Plugin,
                         _ work: @escaping @MainActor () async -> Result<PluginResult, KeryxError>) {
        let name = plugin.name
        working.insert(name)
        failures[name] = nil
        Task {
            if case let .failure(error) = await work() { failures[name] = error.message }
            working.remove(name)
        }
    }
}

/// The owner's own voice tools: what each does and whether it needs the PIN, any file the
/// next call would refuse and why, and the folder they are in.
package struct OwnToolsSection: View {
    let directory: String

    @Environment(AppModel.self) private var model

    package init(directory: String) {
        self.directory = directory
    }

    package var body: some View {
        let assistant = Wording.assistant(model.status.value) ?? "the assistant"
        Section {
            switch (model.tools.value, model.tools.error) {
            case let (report?, _):
                let own = PluginsWording.ownTools(report, plugins: model.plugins.value)
                if own.tools.isEmpty, own.refused.isEmpty {
                    Text("None yet.")
                        .foregroundStyle(.secondary)
                }
                ForEach(own.tools) { tool in
                    LabeledContent {
                        Text(tool.needsPin ? "Needs PIN" : "No PIN")
                            .foregroundStyle(.secondary)
                    } label: {
                        Text(tool.name)
                        Text(PluginsWording.firstSentence(tool.description))
                            .lineLimit(2)
                    }
                    .help(tool.description)
                }
                ForEach(own.refused) { refused in
                    LabeledContent {
                        HStack(spacing: 6) {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundStyle(.orange)
                                .accessibilityHidden(true)
                            Text("Not loading")
                                .foregroundStyle(.secondary)
                        }
                    } label: {
                        Text(refused.file)
                        Text(PluginsWording.refusal(refused.reason))
                            .textSelection(.enabled)
                    }
                }
            case let (nil, error?):
                Text(error.message)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            case (nil, nil):
                HStack {
                    ProgressView().controlSize(.small)
                    Text("Reading the tools folder…").foregroundStyle(.secondary)
                }
            }
            LabeledContent {
                Button("Reveal Tools Folder") { Reveal.inFinder(directory) }
            } label: {
                Text("Tools folder")
                Text((directory as NSString).abbreviatingWithTildeInPath)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(directory)
            }
        } header: {
            Text("Your Own Tools")
        } footer: {
            Text("A tool of your own is a Python file in the tools folder, loaded with each call: no restart. "
                + "To make one, ask \(assistant) on a call or any coding agent; it follows Keryx's custom tools skill.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// Shows a folder in Finder, or the nearest one above it that exists.
enum Reveal {
    static func inFinder(_ path: String) {
        var url = URL(fileURLWithPath: path)
        while !FileManager.default.fileExists(atPath: url.path), url.pathComponents.count > 1 {
            url.deleteLastPathComponent()
        }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }
}
