import AppKit
import KeryxKit
import SwiftUI

/// One setting: its title and a line of description, the control its type wants, and where
/// its value comes from. The whole description and the key are in the tooltip (and Copy
/// Key); a write's error is shown under the title.
struct ConfigRowView: View {
    let row: ConfigRow
    /// Every setting's title by key, to say the settings a description names.
    var titles: [String: String] = [:]

    @Environment(AppModel.self) private var model
    @State private var draft: String
    @State private var pending: JSONValue?
    @State private var writing = false
    @State private var error: KeryxError?
    /// What is wrong with a number field's text, found before it was sent.
    @State private var invalid: String?
    @State private var editingSecret = false
    @State private var confirmingRemoval = false
    /// The last text the CLI refused, so a blur does not send it again unchanged.
    @State private var refused: String?
    @FocusState private var focused: Bool

    init(row: ConfigRow, titles: [String: String] = [:], error: KeryxError? = nil) {
        self.row = row
        self.titles = titles
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
                if let caption {
                    Text(caption)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .help(readOnly ? ConfigWording.environmentHelp(row) : "")
        } label: {
            Text(row.displayTitle)
            if !row.description.isEmpty {
                Text(ConfigWording.described(row.description, titles: titles))
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            if ConfigWording.hasMenu(row), let note = OptionWording.note(model.options[row.key], titles: titles) {
                Text(note)
                    .lineLimit(2)
                    .help(note)
            }
            if let problem {
                Text(problem)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
        }
        // The whole description, and the key the owner types at `keryx config set`.
        .help(ConfigWording.tooltip(row))
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
        .onChange(of: draft) {
            refused = nil
            invalid = nil
        }
        .sheet(isPresented: $editingSecret) {
            SecretSheet(key: row.key, title: row.displayTitle, replacing: row.set)
        }
        .confirmationDialog(ConfigWording.removalTitle(row), isPresented: $confirmingRemoval) {
            Button("Remove", role: .destructive) { write { await model.unset(row.key) } }
        } message: {
            Text(ConfigWording.secretRemovalMessage)
        }
    }

    private var readOnly: Bool { ConfigWording.readOnly(row) }

    /// The line under the control: where the value comes from, or for a secret and the PIN
    /// whether one is set.
    private var caption: String? {
        if ConfigWording.isPIN(row) { return ConfigWording.pinCaption(row, status: model.pin.value) }
        if row.secret { return ConfigWording.secretCaption(row) }
        if ConfigWording.hasMenu(row) {
            let detail = OptionWording.detail(of: ConfigWording.editText(pending ?? row.value),
                                              in: model.options[row.key]?.value)
            let parts = [detail, ConfigWording.source(row.source)].compactMap { $0 }
            return parts.isEmpty ? nil : parts.joined(separator: " · ")
        }
        return ConfigWording.source(row.source)
    }

    /// The local check's sentence, else the CLI's.
    private var problem: String? {
        if let invalid { return invalid }
        return error.map { ConfigWording.errorText($0, for: row) }
    }

    @ViewBuilder private var control: some View {
        if ConfigWording.isPIN(row) {
            pin
        } else if row.secret {
            secret
        } else if ConfigWording.isChecklist(row) {
            checklist
        } else if ConfigWording.hasMenu(row) {
            OptionMenu(key: row.key, title: row.displayTitle, current: ConfigWording.editText(pending ?? row.value)) {
                value in
                pending = value.isEmpty ? .null : .string(value)
                write { await model.choose(row.key, option: value) }
            }
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
                    ForEach(choices, id: \.self) { Text(ConfigWording.choiceTitle($0, for: row)).tag($0) }
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
        if row.set {
            Button("Remove…") { reset() }
                .accessibilityLabel("Remove \(row.displayTitle)")
        }
        Button(row.set ? "Change…" : "Set…") { editingSecret = true }
            .accessibilityLabel(row.set ? "Change \(row.displayTitle)" : "Set \(row.displayTitle)")
    }

    // MARK: - The PIN

    /// Set PIN… or Change PIN…, opening the PIN sheet (`keryx pin set`); its caption says
    /// where the PIN is.
    private var pin: some View {
        PinButton(fallbackSet: row.set)
    }

    // MARK: - A checklist

    /// A checkbox per choice (`AGENTS_ENABLED`). The last one checked cannot be unchecked:
    /// one at least must stay on.
    private var checklist: some View {
        let on = checkedChoices
        return VStack(alignment: .leading, spacing: 4) {
            ForEach(row.choices ?? [], id: \.self) { choice in
                Toggle(ConfigWording.choiceTitle(choice, for: row), isOn: Binding {
                    on.contains(choice)
                } set: { checked in
                    let value = ConfigWording.toggling(choice, on: checked, in: on, choices: row.choices ?? [])
                    pending = .array(value.split(separator: ",").map { .string(String($0)) })
                    write { await model.set(row, to: value) }
                })
                .toggleStyle(.checkbox)
                .disabled(on == [choice])
                .help(on == [choice] ? "At least one stays on" : "")
            }
        }
    }

    /// What the checklist has on. `AGENTS_ENABLED` left blank means the default agent alone.
    private var checkedChoices: [String] {
        let fallback = ConfigWording.agentKeys.contains(row.key)
            ? model.config.value?.first { $0.key == "AGENT_BACKEND" }.map { ConfigWording.editText($0.value) }
            : nil
        return ConfigWording.checked(pending ?? row.value, fallback: fallback)
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
    /// the text the CLI just refused until it is edited. A number that is not one is said so
    /// here, without asking the CLI.
    private func commit() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard ConfigWording.shouldCommit(text, stored: ConfigWording.editText(row.value),
                                         writing: writing, refused: refused) else { return }
        if let reason = ConfigWording.invalid(text, type: row.type) {
            invalid = reason
            error = nil
            return
        }
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
        invalid = nil
        Task {
            let failure = await action()
            error = failure
            refused = failure == nil ? nil : attempt
            pending = nil
            writing = false
        }
    }
}
