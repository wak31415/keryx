import KeryxKit
import SwiftUI

/// "Use This Model…": chooses a downloaded model for the local agent, then offers to start
/// the model server that runs it.
package struct UseModelSheet: View {
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
    @State private var everyTask = false
    @State private var working = false
    @State private var failure: KeryxError?

    package init(entry: CatalogModel, stage: Stage = .asking) {
        self.entry = entry
        _stage = State(initialValue: stage)
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
                    .disabled(working)
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
                    .disabled(working || model.modelServerRun.isBusy)
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

    /// Starts it, and leaves its steps to Local Models' Model Server, scrolled to.
    private func startServer() {
        model.runModelServer()
        model.show(inModels: ModelsAnchor.modelServer)
        dismiss()
    }
}
