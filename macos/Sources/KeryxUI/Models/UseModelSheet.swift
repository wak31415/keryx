import KeryxKit
import SwiftUI

/// "Use This Model…": chooses a downloaded model for the local agent, then offers to start
/// the model server that runs it.
struct UseModelSheet: View {
    enum Stage: Equatable {
        /// Asking, with the choice of whether it becomes the default agent.
        case asking
        /// Chosen; what is left is starting its server and restarting Keryx.
        case chosen
    }

    let entry: CatalogModel

    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var stage: Stage
    @State private var everyTask = false
    @State private var working = false
    @State private var failure: KeryxError?

    init(entry: CatalogModel, stage: Stage = .asking) {
        self.entry = entry
        _stage = State(initialValue: stage)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            switch stage {
            case .asking: asking
            case .chosen: chosen
            }
            if let failure {
                Text(failure.message)
                    .font(.callout)
                    .foregroundStyle(.red)
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
            Text("Keryx will run it with llama.cpp on this Mac and send tasks to it.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        Toggle("Use it for every task unless you name another agent", isOn: $everyTask)
            .disabled(working)
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

    /// The model server is already serving it.
    private var serving: Bool { llm.map { $0.reachable && $0.model == entry.name } ?? false }

    /// The server can be started from here: llama.cpp is installed, and there is a checkout
    /// to install its service from.
    private var canStartServer: Bool {
        llm?.binary != nil && model.location.value?.checkout != nil
    }

    private var chosenDetail: String {
        Self.detail(serving: serving, llamaInstalled: llm?.binary != nil,
                    checkout: model.location.value?.checkout != nil,
                    serverInstalled: llm?.unitInstalled == true, keryxRestart: model.configNeedsRestart)
    }

    /// What is left once a model is chosen, in one or two sentences: loading it into the
    /// model server, then restarting Keryx.
    static func detail(serving: Bool, llamaInstalled: Bool, checkout: Bool, serverInstalled: Bool,
                       keryxRestart: Bool) -> String {
        let keryx = "restart Keryx to start using it"
        guard !serving else { return keryxRestart ? Wording.capitalised(keryx) + "." : "It's running and ready." }
        let server: String
        if !llamaInstalled {
            server = "llama.cpp isn't installed yet: keryx setup installs it and starts the model server."
        } else if !checkout {
            server = "Start its model server with keryx setup."
        } else {
            let verb = serverInstalled ? "Restart" : "Start"
            return keryxRestart ? "\(verb) the model server to load it, then \(keryx)."
                : "\(verb) the model server to load it."
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
                    .disabled(working)
            case .chosen:
                if serving {
                    Button("Done") { dismiss() }
                        .keyboardShortcut(.defaultAction)
                } else if canStartServer {
                    Button("Not Now") { dismiss() }
                        .keyboardShortcut(.cancelAction)
                    Button(llm?.unitInstalled == true ? "Restart Model Server" : "Start Model Server") {
                        startServer()
                    }
                    .keyboardShortcut(.defaultAction)
                    .disabled(working)
                } else {
                    Button("Not Now") { dismiss() }
                        .keyboardShortcut(.cancelAction)
                    OpenSetupButton()
                        .keyboardShortcut(.defaultAction)
                }
            }
        }
    }

    private func use() {
        working = true
        failure = nil
        Task {
            failure = await model.use(model: entry.name, defaultAgent: everyTask)
            working = false
            if failure == nil { stage = .chosen }
        }
    }

    private func startServer() {
        working = true
        failure = nil
        Task {
            failure = await model.startModelServer()
            working = false
            if failure == nil { dismiss() }
        }
    }
}
