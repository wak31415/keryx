import KeryxKit
import SwiftUI

/// Sets or replaces a secret. What is typed lives only in this sheet's state: it reaches the
/// CLI on its stdin (`AppModel.setSecret`), is never shown, logged or put on argv, and is
/// cleared whenever the sheet goes away, saved or not.
struct SecretSheet: View {
    let key: String
    /// The setting's name for a person ("OpenAI API key").
    let title: String
    let description: String
    /// The secret is already set, so saving replaces it.
    let replacing: Bool

    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var value: String
    @State private var saving = false
    @State private var error: KeryxError?

    init(key: String, title: String, description: String, replacing: Bool) {
        self.init(key: key, title: title, description: description, replacing: replacing, typed: "")
    }

    /// `typed`: what the field starts holding — the catalog's dots, never a real secret.
    init(key: String, title: String, description: String, replacing: Bool, typed: String) {
        self.key = key
        self.title = title
        self.description = description
        self.replacing = replacing
        _value = State(initialValue: typed)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                Text(replacing ? "Change \(title)" : "Set \(title)")
                    .font(.headline)
                Text(ConfigWording.description(description))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding([.horizontal, .top], 20)

            Form {
                Section {
                    SecureField("Value", text: $value, prompt: Text(replacing ? "New value" : "Paste or type it"))
                        .onSubmit(save)
                } footer: {
                    VStack(alignment: .leading, spacing: 4) {
                        if let error {
                            Text(ConfigWording.errorText(error.message, key: key, title: title))
                                .foregroundStyle(.red)
                                .textSelection(.enabled)
                        }
                        Text("Kept in secrets.toml, readable only by you. Keryx never shows it again.")
                            .foregroundStyle(.secondary)
                    }
                    .font(.callout)
                }
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            .scrollDisabled(true)
            .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 8) {
                if saving {
                    ProgressView().controlSize(.small)
                }
                Spacer()
                Button("Cancel", role: .cancel) { close() }
                    .keyboardShortcut(.cancelAction)
                    // The save would still land after the sheet had gone.
                    .disabled(saving)
                Button("Save", action: save)
                    .keyboardShortcut(.defaultAction)
                    .disabled(value.isEmpty || saving)
            }
            .padding([.horizontal, .bottom], 20)
        }
        .frame(width: 460)
        .onDisappear { value = "" }
    }

    private func save() {
        guard !value.isEmpty, !saving else { return }
        saving = true
        error = nil
        Task {
            let failure = await model.setSecret(key, value: value)
            saving = false
            if let failure {
                error = failure
            } else {
                close()
            }
        }
    }

    private func close() {
        value = ""
        dismiss()
    }
}
