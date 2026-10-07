import KeryxKit
import SwiftUI

/// Connects a Twilio account, or switches to another: the account SID and the auth token,
/// checked together with Twilio (`config verify TWILIO_AUTH_TOKEN --stdin --pair`, both on
/// stdin, neither saved) before they are saved — the SID as a setting, the token as a
/// secret on `config set --stdin`. A pair Twilio refused may still be saved, by a Save
/// Anyway that says so. The token lives only in this sheet's state, never shown, logged or
/// put on argv, and is emptied whenever the sheet goes.
package struct TwilioAccountSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var sid: String
    @State private var token: String
    @State private var check: SecretCheck
    @State private var saving = false
    /// What went wrong saving, in sentences.
    @State private var failure: String?

    /// `savedSID`: the account SID saved now, which the field starts with. `token`: the
    /// catalog's dots, never a real token.
    package init(savedSID: String?, token: String = "", check: SecretCheck = .unchecked) {
        _sid = State(initialValue: savedSID ?? "")
        _token = State(initialValue: token)
        _check = State(initialValue: check)
    }

    package var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Twilio Account")
                .font(.headline)
                .padding([.horizontal, .top], 20)

            Form {
                Section {
                    TextField("Account SID", text: $sid, prompt: Text("AC…"))
                        .autocorrectionDisabled()
                    SecureField("Auth token", text: $token, prompt: Text("Paste it from Twilio's console"))
                        // Return saves, but never anyway: that takes a click.
                        .onSubmit { if !check.savesAnyway { save() } }
                } footer: {
                    VStack(alignment: .leading, spacing: 4) {
                        if let problem = PhoneWording.sidProblem(sid) {
                            Text(problem).foregroundStyle(.red)
                        }
                        if let said = PhoneWording.accountCheck(check) {
                            Label {
                                Text(said.text)
                            } icon: {
                                Image(systemName: said.works ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                                    .foregroundStyle(said.works ? .green : .orange)
                            }
                            .textSelection(.enabled)
                        }
                        if let failure {
                            Text(failure)
                                .foregroundStyle(.red)
                                .textSelection(.enabled)
                        }
                        Text("Both are checked with Twilio before they're saved. The token is kept in "
                            + "secrets.toml, readable only by you; Keryx never shows it again.")
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
                if busy {
                    ProgressView().controlSize(.small)
                    Text(check == .checking ? "Checking with Twilio…" : "Saving…").foregroundStyle(.secondary)
                }
                Spacer()
                Button("Cancel", role: .cancel) { close() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(saving)
                Button("Verify") { verify(thenSave: false) }
                    .disabled(!complete || busy)
                    .help("Checks the pair with Twilio, without saving either")
                Button(check.saveTitle, action: save)
                    .keyboardShortcut(check.savesAnyway ? nil : .defaultAction)
                    .disabled(!complete || busy)
            }
            .padding([.horizontal, .bottom], 20)
        }
        .frame(width: 500)
        .onChange(of: sid) { typedOver() }
        .onChange(of: token) { typedOver() }
        .onDisappear { token = "" }
    }

    private var trimmedSID: String { sid.trimmingCharacters(in: .whitespacesAndNewlines) }

    /// Both are there, and the SID is one.
    private var complete: Bool {
        !trimmedSID.isEmpty && !token.isEmpty && PhoneWording.sidProblem(sid) == nil
    }

    private var busy: Bool { saving || check == .checking }

    private func typedOver() {
        if check != .checking { check = .unchecked }
        failure = nil
    }

    private func verify(thenSave: Bool) {
        guard complete, !busy else { return }
        let pair = (trimmedSID, token)
        check = .checking
        failure = nil
        Task {
            let answer = await model.verifyTwilio(accountSID: pair.0, authToken: pair.1)
            guard pair == (trimmedSID, token) else {
                check = .unchecked
                return
            }
            check = SecretCheck(answer)
            if thenSave, case .works = check { save() }
        }
    }

    /// Checks first; once Twilio has accepted the pair (or the owner saves it anyway), the
    /// SID and then the token.
    private func save() {
        guard complete, !busy else { return }
        if check.mustCheckFirst {
            verify(thenSave: true)
            return
        }
        let pair = (trimmedSID, token)
        let savedSID = model.config.value?.first { $0.key == "TWILIO_ACCOUNT_SID" }
            .map { ConfigWording.editText($0.value) }
        saving = true
        failure = nil
        Task {
            let sidChanged = pair.0 != savedSID
            if sidChanged, let error = await model.set(key: "TWILIO_ACCOUNT_SID", to: pair.0) {
                saving = false
                failure = PhoneWording.saveFailure(error)
                return
            }
            if let error = await model.setSecret("TWILIO_AUTH_TOKEN", value: pair.1) {
                saving = false
                failure = sidChanged ? PhoneWording.tokenNotSaved(error) : PhoneWording.saveFailure(error)
                // The new SID is saved: show it, and read what Twilio says of the pair now.
                if sidChanged { await model.refresh(.phoneNumbers, .phoneWebhooks) }
                return
            }
            saving = false
            close()
            await model.refresh(.phoneNumbers, .phoneWebhooks, .setupPlan)
        }
    }

    private func close() {
        token = ""
        dismiss()
    }
}
