import AppKit
import KeryxKit
import SwiftUI

/// One setting: its key and description, where its value comes from, and the control its
/// type wants. A write's error is shown under it, in the CLI's own sentence.
struct ConfigRowView: View {
    let row: ConfigRow

    @Environment(AppModel.self) private var model
    @State private var draft: String
    @State private var pending: JSONValue?
    @State private var writing = false
    @State private var error: KeryxError?
    @State private var editingSecret = false
    @FocusState private var focused: Bool

    init(row: ConfigRow, error: KeryxError? = nil) {
        self.row = row
        _draft = State(initialValue: ConfigWording.editText(row.value))
        _error = State(initialValue: error)
    }

    var body: some View {
        LabeledContent {
            VStack(alignment: .trailing, spacing: 4) {
                HStack(spacing: 8) {
                    if writing {
                        ProgressView().controlSize(.small)
                    }
                    control
                        .disabled(readOnly || writing)
                }
                if let source = ConfigWording.source(row.source) {
                    Text(source)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .help(readOnly ? ConfigWording.environmentHelp : "")
        } label: {
            Text(row.key)
                .textSelection(.enabled)
            Text(ConfigWording.description(row.description))
                .lineLimit(2)
                .help(ConfigWording.plain(row.description))
            if let error {
                Text(error.message)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
        }
        .contextMenu {
            Button("Reset to Default") { write { await model.unset(row.key) } }
                .disabled(!row.set || readOnly)
            Button("Copy Key") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(row.key, forType: .string)
            }
        }
        .onChange(of: row.value) { draft = ConfigWording.editText(row.value) }
        .sheet(isPresented: $editingSecret) {
            SecretSheet(key: row.key, description: row.description, replacing: row.set)
        }
    }

    private var readOnly: Bool { ConfigWording.readOnly(row) }

    @ViewBuilder private var control: some View {
        if row.secret {
            secret
        } else {
            switch row.type {
            case "bool":
                Toggle(row.key, isOn: boolBinding)
                    .toggleStyle(.switch)
                    .labelsHidden()
            case "choice":
                Picker(row.key, selection: choiceBinding) {
                    if ConfigWording.editText(row.value).isEmpty {
                        Text("Not set").tag("")
                    }
                    ForEach(choices, id: \.self) { Text($0).tag($0) }
                }
                .pickerStyle(.menu)
                .labelsHidden()
                .fixedSize()
            case "int", "float":
                field(width: 100)
                    .monospacedDigit()
                    .multilineTextAlignment(.trailing)
            default:
                field(width: 240)
                    .multilineTextAlignment(.leading)
            }
        }
    }

    // MARK: - Secrets

    @ViewBuilder private var secret: some View {
        Text(row.set ? "Set" : "Not set")
            .foregroundStyle(row.set ? .primary : .secondary)
        if row.set {
            Button("Remove") { write { await model.unset(row.key) } }
        }
        Button(row.set ? "Change…" : "Set…") { editingSecret = true }
    }

    // MARK: - Values

    private var boolBinding: Binding<Bool> {
        Binding {
            if case let .bool(on)? = pending ?? row.value { return on }
            return false
        } set: { on in
            pending = .bool(on)
            write { await model.set(row, to: on ? "true" : "false") }
        }
    }

    /// The choices, and the current value too when the CLI names one it does not list.
    private var choices: [String] {
        let listed = row.choices ?? []
        let current = ConfigWording.editText(row.value)
        return current.isEmpty || listed.contains(current) ? listed : [current] + listed
    }

    private var choiceBinding: Binding<String> {
        Binding {
            ConfigWording.editText(pending ?? row.value)
        } set: { choice in
            guard !choice.isEmpty else { return }
            pending = .string(choice)
            write { await model.set(row, to: choice) }
        }
    }

    private func field(width: CGFloat) -> some View {
        TextField(row.key, text: $draft, prompt: Text("Not set"))
            .labelsHidden()
            .textFieldStyle(.roundedBorder)
            .frame(width: width)
            .focused($focused)
            .onSubmit(commit)
            .onChange(of: focused) { if !focused { commit() } }
            .help(readOnly ? ConfigWording.environmentHelp : draft)
    }

    /// Saves the field's text when it differs from what is stored.
    private func commit() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard text != ConfigWording.editText(row.value) else { return }
        write { await model.set(row, to: text) }
    }

    /// Runs one write, shows its error (or clears the last one), and lets the control follow
    /// the reloaded row again.
    private func write(_ action: @escaping () async -> KeryxError?) {
        writing = true
        Task {
            let failure = await action()
            error = failure
            pending = nil
            writing = false
        }
    }
}
