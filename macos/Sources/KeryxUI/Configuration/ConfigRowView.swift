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
    @State private var confirmingRemoval = false
    /// The last text the CLI refused, so a blur does not send it again unchanged.
    @State private var refused: String?
    @State private var launchFailure: KeryxError?
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
                        .disabled(!ConfigWording.isPIN(row) && (readOnly || writing))
                }
                if ConfigWording.isPIN(row) {
                    Text(ConfigWording.pinNote)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if let source = ConfigWording.source(row.source) {
                    Text(source)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .help(readOnly ? ConfigWording.environmentHelp(row) : "")
        } label: {
            Text(row.displayTitle)
            Text(ConfigWording.description(row.description))
                .lineLimit(2)
                .help(ConfigWording.plain(row.description))
            // What the owner types at `keryx config set`.
            Text(row.key)
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
            if let error {
                Text(ConfigWording.errorText(error.message, for: row))
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
        }
        .contextMenu {
            if ConfigWording.offersWrites(row) {
                Button("Reset to Default") { reset() }
                    .disabled(!row.set || writing)
            }
            Button("Copy Key") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(row.key, forType: .string)
            }
        }
        .onChange(of: row.value) { draft = ConfigWording.editText(row.value) }
        .onChange(of: draft) { refused = nil }
        .sheet(isPresented: $editingSecret) {
            SecretSheet(key: row.key, title: row.displayTitle, description: row.description, replacing: row.set)
        }
        .alert(launchFailure?.title ?? "", isPresented: Binding(
            get: { launchFailure != nil }, set: { if !$0 { launchFailure = nil } }
        )) {
            Button("OK") {}
        } message: {
            Text(launchFailure?.message ?? "")
        }
        .confirmationDialog(ConfigWording.removalTitle(row), isPresented: $confirmingRemoval) {
            Button("Remove", role: .destructive) { write { await model.unset(row.key) } }
        } message: {
            Text(ConfigWording.secretRemovalMessage)
        }
    }

    private var readOnly: Bool { ConfigWording.readOnly(row) }

    @ViewBuilder private var control: some View {
        if ConfigWording.isPIN(row) {
            pin
        } else if row.secret {
            secret
        } else {
            switch row.type {
            case "bool":
                Toggle(row.displayTitle, isOn: boolBinding)
                    .toggleStyle(.switch)
                    .labelsHidden()
            case "choice":
                Picker(row.displayTitle, selection: choiceBinding) {
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
            Button("Remove") { reset() }
                .accessibilityLabel("Remove \(row.displayTitle)")
        }
        Button(row.set ? "Change…" : "Set…") { editingSecret = true }
            .accessibilityLabel(row.set ? "Change \(row.displayTitle)" : "Set \(row.displayTitle)")
    }

    // MARK: - The PIN

    /// Whether a PIN is set, and the way to change one; nothing here writes it.
    @ViewBuilder private var pin: some View {
        Text(row.set ? "Set" : "Not set")
            .foregroundStyle(row.set ? .primary : .secondary)
        Button("Open Setup in Terminal") { openSetup() }
            .disabled(model.location.value == nil)
    }

    private func openSetup() {
        guard let location = model.location.value else { return }
        do {
            try TerminalLauncher.open(location, arguments: ["setup"])
        } catch {
            launchFailure = (error as? KeryxError) ?? .launch(error.localizedDescription)
        }
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
        TextField(row.displayTitle, text: $draft, prompt: Text("Not set"))
            .labelsHidden()
            .textFieldStyle(.roundedBorder)
            .frame(width: width)
            .focused($focused)
            .onSubmit(commit)
            .onChange(of: focused) { if !focused { commit() } }
            .help(readOnly ? ConfigWording.environmentHelp(row) : draft)
    }

    /// Saves the field's text when it differs from what is stored — once: not while a write
    /// is under way (disabling the field drops its focus, which would commit again), and not
    /// the text the CLI just refused until it is edited.
    private func commit() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard ConfigWording.shouldCommit(text, stored: ConfigWording.editText(row.value),
                                         writing: writing, refused: refused) else { return }
        write(attempt: text) { await model.set(row, to: text) }
    }

    /// Back to the default: at once for a plain setting, after asking for a secret, which
    /// cannot be shown again and would have to be pasted in again.
    private func reset() {
        if ConfigWording.resetNeedsConfirmation(row) {
            confirmingRemoval = true
        } else {
            write { await model.unset(row.key) }
        }
    }

    /// Runs one write, shows its error (or clears the last one), and lets the control follow
    /// the reloaded row again. `attempt`: a field's text, remembered when it is refused.
    private func write(attempt: String? = nil, _ action: @escaping () async -> KeryxError?) {
        guard !writing else { return }
        writing = true
        Task {
            let failure = await action()
            error = failure
            refused = failure == nil ? nil : attempt
            pending = nil
            writing = false
        }
    }
}
