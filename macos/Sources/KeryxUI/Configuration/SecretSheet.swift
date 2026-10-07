import KeryxKit
import SwiftUI

/// Sets or replaces a secret. What is typed lives only in this sheet's state: it reaches the
/// CLI on its stdin (`AppModel.setSecret`), is never shown, logged or put on argv, and is
/// cleared whenever the sheet goes away, saved or not.
///
/// With `keep`, the sheet saves nothing itself: it hands what was typed to `keep`, for a
/// command that takes the secret on its own stdin (`keryx voice use server --key-stdin`).
///
/// A key Keryx can check (`SecretCheck.keys`) is checked before it is saved, with
/// `config verify KEY --stdin`: Save checks first and saves once the key works; a key that
/// does not is said why, and Save becomes Save Anyway. Verify checks without saving.
package struct SecretSheet: View {
    let key: String
    /// The setting's name for a person ("OpenAI API key"), which labels the field.
    let title: String
    /// The secret is already set, so saving replaces it.
    let replacing: Bool
    /// Where the secret goes instead of `config set --stdin`, and what the footer says of it.
    private let keep: ((String) -> Void)?
    private let footnote: String

    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var value: String
    @State private var saving = false
    @State private var error: KeryxError?
    @State private var check: SecretCheck
    /// The check under way, stopped if the sheet goes first.
    @State private var verifying: Task<Void, Never>?

    init(key: String, title: String, replacing: Bool) {
        self.init(key: key, title: title, replacing: replacing, typed: "")
    }

    static let stored = "Kept in secrets.toml, readable only by you. Keryx never shows it again."

    /// `typed`: what the field starts holding — the catalog's dots, never a real secret.
    /// `check`: where the check starts — the catalog's states; otherwise not yet checked.
    package init(key: String, title: String, replacing: Bool, typed: String,
                 footnote: String = SecretSheet.stored, check: SecretCheck = .unchecked,
                 keep: ((String) -> Void)? = nil) {
        self.key = key
        self.title = title
        self.replacing = replacing
        self.keep = keep
        self.footnote = footnote
        _value = State(initialValue: typed)
        _check = State(initialValue: check)
    }

    /// Whether this sheet checks its key before saving it.
    private var checks: Bool { SecretCheck.applies(to: key) }

    package var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(replacing ? "Change \(title)" : "Set \(title)")
                .font(.headline)
                .padding([.horizontal, .top], 20)

            Form {
                Section {
                    SecureField(title, text: $value, prompt: Text(replacing ? "New value" : "Paste or type it"))
                        // Return saves, but never anyway: that takes a click.
                        .onSubmit { if !savesAnyway { save() } }
                } footer: {
                    VStack(alignment: .leading, spacing: 4) {
                        if checks, let said = check.said(key: key) {
                            Label {
                                Text(said.text)
                            } icon: {
                                Image(systemName: said.works ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                                    .foregroundStyle(said.works ? .green : .orange)
                            }
                            .textSelection(.enabled)
                        }
                        if let error {
                            Text(error.reported.map { ConfigWording.errorText($0, key: key, title: title) }
                                ?? error.message)
                                .foregroundStyle(.red)
                                .textSelection(.enabled)
                        }
                        Text(footnote)
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
                if saving || check == .checking {
                    ProgressView().controlSize(.small)
                    if check == .checking {
                        Text("Checking…").foregroundStyle(.secondary)
                    }
                }
                Spacer()
                Button("Cancel", role: .cancel) { close() }
                    .keyboardShortcut(.cancelAction)
                    // The save would still land after the sheet had gone.
                    .disabled(saving)
                if checks {
                    Button("Verify") { verify(thenSave: false) }
                        .disabled(value.isEmpty || busy)
                        .help("Checks the key with its provider, without saving it")
                }
                Button(checks ? check.saveTitle : "Save", action: save)
                    .keyboardShortcut(savesAnyway ? nil : .defaultAction)
                    .disabled(value.isEmpty || busy)
            }
            .padding([.horizontal, .bottom], 20)
        }
        .frame(width: 460)
        .onChange(of: value) { if check != .checking { check = .unchecked } }
        // A save under way would land after the sheet had gone: it stays until it has.
        .interactiveDismissDisabled(saving)
        .onDisappear {
            stopVerifying()
            value = ""
        }
    }

    private var busy: Bool { saving || check == .checking }

    /// Save would save a key that did not work.
    private var savesAnyway: Bool { checks && check.savesAnyway }

    /// Asks Keryx whether what is typed works, saving nothing; `thenSave`: and saves it
    /// when it does.
    private func verify(thenSave: Bool) {
        guard !value.isEmpty, !busy else { return }
        let typed = value
        check = .checking
        error = nil
        verifying = Task {
            let answer = await model.verify(key: key, value: typed)
            // Cancelled (the sheet went): nothing more, and nothing said.
            guard !Task.isCancelled else { return }
            verifying = nil
            // Typed over while it was asked: that answer is about something else.
            guard typed == value else {
                check = .unchecked
                return
            }
            check = SecretCheck(answer)
            if thenSave, case .works = check { save() }
        }
    }

    private func save() {
        guard !value.isEmpty, !busy else { return }
        if checks, check.mustCheckFirst {
            verify(thenSave: true)
            return
        }
        if let keep {
            keep(value)
            close()
            return
        }
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

    /// Stops a check under way: its child is stopped, and with it the copy of the key the
    /// call holds.
    private func stopVerifying() {
        verifying?.cancel()
        verifying = nil
        if check == .checking { check = .unchecked }
    }

    private func close() {
        stopVerifying()
        value = ""
        dismiss()
    }
}

/// Where checking a key before it is saved has got to (`keryx config verify KEY --stdin`),
/// and what the secret sheet says and offers there.
package enum SecretCheck: Equatable, Sendable {
    /// Not asked yet, or typed over since.
    case unchecked
    case checking
    /// Its provider accepted it; `detail` is what Keryx said of it, if anything.
    case works(detail: String?)
    /// It did not work, or could not be checked: one sentence of why.
    case problem(String)

    /// The keys the secret sheet checks before saving. Twilio's token is checked with its
    /// account SID, in the Phone section's account sheet.
    package static let keys: Set<String> = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_API_KEY"]

    package static func applies(to key: String) -> Bool { keys.contains(key) }

    /// The answer `AppModel.verify(key:value:)` gave. Keryx failing to check at all is a
    /// problem too: the owner may still save the key.
    package init(_ answer: Result<VerifyResult, KeryxError>) {
        switch answer {
        case let .success(result) where result.ok:
            self = .works(detail: result.detail)
        case let .success(result):
            self = .problem(KeryxError.sentence(result.problem ?? "The key didn't work"))
        case let .failure(error):
            self = .problem(error.reported.map(KeryxError.sentence) ?? error.message)
        }
    }

    /// Save checks first while nothing has been said of what is typed.
    package var mustCheckFirst: Bool { self == .unchecked }

    /// Save would save what has a problem: the owner's call, by a click, never Return.
    package var savesAnyway: Bool {
        if case .problem = self { return true }
        return false
    }

    /// Save once the key has a problem: saving it is the owner's call.
    package var saveTitle: String { savesAnyway ? "Save Anyway" : "Save" }

    /// The line under the field, once there is something to say.
    package func said(key: String) -> (text: String, works: Bool)? {
        switch self {
        case .unchecked, .checking:
            return nil
        case let .works(detail):
            let accepted = "\(Self.provider(key)) accepted the key."
            return (detail.map { "\(accepted) \(KeryxError.sentence($0))" } ?? accepted, true)
        case let .problem(sentence):
            return (sentence, false)
        }
    }

    /// Who checks `key`.
    static func provider(_ key: String) -> String {
        key == "ANTHROPIC_API_KEY" ? "Anthropic" : "OpenAI"
    }
}
