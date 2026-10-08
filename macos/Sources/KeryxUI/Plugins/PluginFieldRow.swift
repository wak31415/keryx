import KeryxKit
import SwiftUI

/// One of a plugin's settings, edited as its type says: a text or number field (saved on
/// Return or when it loses focus), a switch, a menu of choices, or the cluster table.
struct PluginFieldRow: View {
    /// Where the SSH config is, for a tooltip: the copy says "your SSH config".
    static let sshConfigPath = "~/.ssh/config"

    let field: PluginField
    /// What it holds now: the plugin's file, or an off plugin's draft.
    let value: String
    let problem: String?
    let disabled: Bool
    /// The typed value, and whether it was sent because the row was already gone (a
    /// section switch), rather than Return or a blur — the caller has no row left to show
    /// a refusal on then, and shows it as a window-level banner instead.
    let commit: (String, Bool) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            editor
            if let problem {
                Text(problem)
                    .font(.callout)
                    .problemStyle()
            }
        }
        .disabled(disabled)
    }

    @ViewBuilder private var editor: some View {
        switch field.kind {
        case .bool:
            Toggle(field.title, isOn: Binding(get: { value == "true" }, set: { commit($0 ? "true" : "false", false) }))
        case .choice:
            Picker(field.title, selection: Binding(get: { value }, set: { commit($0, false) })) {
                // A value the file holds that is not a choice still shows, as itself.
                if !(field.choices ?? []).contains(value) { Text(value).tag(value) }
                ForEach(field.choices ?? [], id: \.self) { choice in
                    Text(PluginsWording.choiceTitle(choice)).tag(choice)
                }
            }
        case .string, .int, .float, .map, nil:
            CommittingField(title: field.title, value: value, numeric: field.kind == .int || field.kind == .float,
                            commit: commit)
        }
    }
}

/// A text field that hands on what was typed when Return is pressed, it loses focus, or it
/// disappears (a Plugins section switch tears it down before a blur could), and follows the
/// value it was given when that changes underneath it.
private struct CommittingField: View {
    let title: String
    let value: String
    let numeric: Bool
    let commit: (String, Bool) -> Void

    @State private var text: String
    @FocusState private var focused: Bool

    init(title: String, value: String, numeric: Bool, commit: @escaping (String, Bool) -> Void) {
        self.title = title
        self.value = value
        self.numeric = numeric
        self.commit = commit
        _text = State(initialValue: value)
    }

    var body: some View {
        TextField(title, text: $text, prompt: Text(numeric ? "0" : "Not set"))
            .multilineTextAlignment(numeric ? .trailing : .leading)
            .focused($focused)
            .onSubmit { send(onDisappear: false) }
            .onChange(of: focused) { _, now in
                if !now { send(onDisappear: false) }
            }
            .onDisappear { send(onDisappear: true) }
            .onChange(of: value) { _, now in
                if !focused { text = now }
            }
    }

    private func send(onDisappear: Bool) {
        guard text != value else { return }
        commit(text, onDisappear)
    }
}

/// Cluster stats' `[clusters]` table: each ssh host it asks, with its GPU partition, and a
/// row to add one of the hosts with a ControlMaster in ~/.ssh/config.
struct ClusterTableRows: View {
    let field: PluginField
    let table: [String: String]
    let hosts: SshHostsReport?
    /// The last host cannot be removed from a plugin that is on: the table may not be
    /// empty, and the CLI is never handed one.
    let keepsOne: Bool
    let disabled: Bool
    let commit: ([String: String]) -> Void

    @State private var host = ""
    @State private var partition = ""

    var body: some View {
        let available = PluginsWording.clusterHosts(hosts, taken: Set(table.keys))
        Group {
            if table.isEmpty {
                LabeledContent(field.title) {
                    Text("None yet")
                        .foregroundStyle(.secondary)
                }
            }
            ForEach(table.keys.sorted(), id: \.self) { alias in
                LabeledContent {
                    HStack(spacing: 8) {
                        Text(table[alias] ?? "")
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                        Button {
                            var rest = table
                            rest[alias] = nil
                            commit(rest)
                        } label: {
                            Image(systemName: "minus.circle")
                        }
                        .buttonStyle(.borderless)
                        .help("Remove \(alias)")
                        .accessibilityLabel("Remove \(alias)")
                        .disabled(keepsOne && table.count == 1)
                    }
                } label: {
                    Text(alias)
                    Text(sshDetail(alias))
                }
            }
            if available.isEmpty {
                Text(hosts == nil ? "Reading the hosts in your SSH config…"
                    : "No other host in your SSH config has a ControlMaster. Cluster stats only asks over an ssh "
                        + "login you already have open, never one of its own.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .help(PluginFieldRow.sshConfigPath)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                LabeledContent {
                    HStack(spacing: 8) {
                        Picker("Host", selection: $host) {
                            Text("Choose…").tag("")
                            ForEach(available) { candidate in
                                Text(candidate.alias).tag(candidate.alias)
                            }
                        }
                        .labelsHidden()
                        .fixedSize()
                        TextField("Partition", text: $partition, prompt: Text("Partition"))
                            .labelsHidden()
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 110)
                            .onSubmit(add)
                        Button("Add", action: add)
                            .disabled(!canAdd)
                    }
                } label: {
                    Text("Add a host")
                    Text("One with a ControlMaster in your SSH config, and its GPU partition")
                        .help(PluginFieldRow.sshConfigPath)
                }
            }
        }
        .disabled(disabled)
    }

    private var canAdd: Bool {
        !host.isEmpty && PluginsWording.isBareWord(partition.trimmingCharacters(in: .whitespaces))
    }

    private func add() {
        guard canAdd else { return }
        var next = table
        next[host.lowercased()] = partition.trimmingCharacters(in: .whitespaces)
        host = ""
        partition = ""
        commit(next)
    }

    /// "ada@login.cluster.example.edu · signed in": whether its master is up now.
    private func sshDetail(_ alias: String) -> String {
        guard let found = hosts?.hosts.first(where: { $0.alias.lowercased() == alias }) else {
            return "Not in your SSH config"
        }
        let login = found.user.isEmpty ? found.hostname : "\(found.user)@\(found.hostname)"
        return "\(login) · \(found.masterAlive ? "connection open" : "no open connection")"
    }
}
