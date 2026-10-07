import KeryxKit
import SwiftUI

/// Sets the phone PIN, or replaces it: where it is now (`keryx pin`), then the new digits
/// typed twice and compared here before they go, twice, on the stdin of `keryx pin set
/// --stdin [--replace]`. The digits are never shown, logged or put on argv; they live only
/// in this sheet's two fields, which are emptied once the PIN is saved and whenever the
/// sheet goes away. While a save runs the sheet cannot be closed (Cancel is off, and so is
/// Escape), so no PIN is set after it has gone; should it go anyway, with its window, the
/// save is stopped.
///
/// Open it through `pinSheet(isPresented:)` (or `PinButton`), which asks "Are you sure?"
/// before a working PIN is replaced; the sheet then asks "Replace the PIN?" once the new
/// digits are typed, with Cancel as the default, before anything replaces what
/// `KERYX_HOME/pin` holds.
package struct PinSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var first: String
    @State private var second: String
    @State private var submitted = false
    @State private var saving = false
    /// A refusal about the digits (exit 2), said under the fields.
    @State private var digitsProblem: String?
    /// A refusal about this Mac (exit 1), or Keryx failing, said above them.
    @State private var failure: String?
    @State private var saved: PinSetResult?
    /// "Replace the PIN?" is up: the second yes, after the digits and before they go.
    @State private var confirmingReplace = false
    /// The save under way: the only other holder of the digits, until it answers.
    @State private var saveTask: Task<Void, Never>?
    /// The owner said yes, in this sheet, to changing the PIN it found set (after a refusal
    /// showed one was).
    @State private var changeAgreed = false
    @State private var askingChange = false
    @State private var migrateFailure: KeryxError?
    @FocusState private var focus: Field?
    /// The owner has said yes to replacing a working PIN (`PinFlow`'s question). Without
    /// it, a working PIN is shown, never offered for replacing.
    private let replaceConfirmed: Bool

    private enum Field { case first, second }

    /// Only `PinFlow` opens it empty, after its question.
    fileprivate init(replaceConfirmed: Bool) {
        self.init(replaceConfirmed: replaceConfirmed, first: "", second: "")
    }

    /// The catalog's states: `first`/`second` are what the fields start holding (made-up
    /// digits, shown as dots), `failure` a refusal already said.
    package init(replaceConfirmed: Bool, first: String, second: String, submitted: Bool = false,
                 failure: String? = nil, saved: PinSetResult? = nil) {
        self.replaceConfirmed = replaceConfirmed
        _first = State(initialValue: first)
        _second = State(initialValue: second)
        _submitted = State(initialValue: submitted)
        _failure = State(initialValue: failure)
        _saved = State(initialValue: saved)
    }

    package var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(title)
                .font(.headline)
                .padding([.horizontal, .top], 20)

            Form {
                if let saved {
                    Section {
                        Label {
                            Text(PinWording.saved(replaced: saved.replaced, restartNeeded: saved.restartNeeded))
                        } icon: {
                            Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                        }
                    }
                } else if let status = model.pin.value {
                    state(status)
                    if editable(status) {
                        fields(status)
                    }
                } else if let error = model.pin.error, !model.pin.isLoading {
                    Section {
                        Label {
                            Text(error.message).textSelection(.enabled)
                        } icon: {
                            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                        }
                    }
                } else {
                    Section {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("Checking the PIN…").foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            .scrollDisabled(true)
            .fixedSize(horizontal: false, vertical: true)

            buttons
                .padding([.horizontal, .bottom], 20)
        }
        .frame(width: 480)
        .task { await model.refresh(.pin) }
        .onChange(of: first) { edited() }
        .onChange(of: second) { edited() }
        // A save under way stays in front until it has answered.
        .interactiveDismissDisabled(saving)
        .onDisappear {
            // Gone with its window mid-save: stopped, so nothing lands after it.
            saveTask?.cancel()
            saveTask = nil
            forget()
        }
        .alert(PinWording.replaceTitle, isPresented: $confirmingReplace) {
            // Cancel is the default: Return never replaces a PIN.
            Button("Cancel", role: .cancel) {}
                .keyboardShortcut(.defaultAction)
            Button("Replace PIN", role: .destructive) {
                if let status = model.pin.value { save(status, agreed: true) }
            }
        } message: {
            Text(PinWording.replaceMessage)
        }
    }

    private var title: String {
        guard saved == nil, let status = model.pin.value, editable(status) else { return "Phone PIN" }
        return PinWording.sheetTitle(status)
    }

    /// The fields are offered: the app may write a PIN here, and a working one only after
    /// the owner has said yes to replacing it.
    private func editable(_ status: PinStatus) -> Bool {
        PinWording.canSet(status) && (replaceConfirmed || changeAgreed || !PinWording.asksFirst(status))
    }

    // MARK: - Where the PIN is

    private func state(_ status: PinStatus) -> some View {
        let banner = PinWording.banner(status, assistant: Wording.assistant(model.status.value))
        return Section {
            Label {
                VStack(alignment: .leading, spacing: 2) {
                    Text(banner.title).fontWeight(.medium)
                    Text(banner.text)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } icon: {
                Image(systemName: banner.symbol)
                    .foregroundStyle(colour(banner.tone))
                    .font(.title3)
            }
            .padding(.vertical, 2)
            if let terminal = banner.terminal {
                DetailsDisclosure(terminal, heading: "For the Terminal")
            }
            if PinWording.kind(status) == .jarvis {
                LabeledContent {
                    Button("Migrate…") { migrate() }
                        .disabled(model.location.value == nil)
                } label: {
                    Text("Move Keryx's files from Jarvis")
                }
            }
            if let lock = PinWording.lockLine(status) {
                Label {
                    Text(lock)
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "hand.raised.fill").foregroundStyle(.orange)
                }
            }
            if let failure {
                Label {
                    Text(failure)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
                }
            }
        }
    }

    private func colour(_ tone: PinWording.Tone) -> Color {
        switch tone {
        case .ok: .green
        case .attention: .orange
        case .blocked: .red
        }
    }

    // MARK: - The digits

    private func fields(_ status: PinStatus) -> some View {
        Section {
            SecureField("New PIN", text: $first, prompt: Text("Required"))
                .focused($focus, equals: .first)
                .onSubmit { focus = .second }
            SecureField("Confirm PIN", text: $second, prompt: Text("Required"))
                .focused($focus, equals: .second)
                .onSubmit { save(status) }
        } footer: {
            Group {
                if let problem {
                    Label {
                        Text(problem).foregroundStyle(.secondary)
                    } icon: {
                        Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.red)
                    }
                } else {
                    Text(PinWording.rule).foregroundStyle(.secondary)
                }
            }
            .font(.callout)
        }
        .disabled(saving)
    }

    /// Theirs first (what the CLI refused), else what is typed.
    private var problem: String? {
        digitsProblem ?? PinWording.problem(first: first, second: second, submitted: submitted)
    }

    // MARK: - Buttons

    @ViewBuilder private var buttons: some View {
        HStack(spacing: 8) {
            if saving {
                ProgressView().controlSize(.small)
                Text("Saving…").foregroundStyle(.secondary)
            }
            Spacer()
            if let status = model.pin.value, saved == nil, editable(status) {
                Button("Cancel", role: .cancel) { close() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(saving)
                Button(PinWording.actionTitle(status)) { save(status) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(saving || first.isEmpty || second.isEmpty)
            } else if let status = model.pin.value, saved == nil, PinWording.canSet(status),
                      PinWording.asksFirst(status) {
                // A PIN is set (perhaps found so only now, by a refusal): Change it from here.
                Button("Done") { close() }
                    .keyboardShortcut(.cancelAction)
                Button("Change PIN…") { askingChange = true }
            } else {
                Button("Done") { close() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .confirmationDialog("Change the Phone PIN?", isPresented: $askingChange) {
            Button("Change PIN…", role: .destructive) {
                changeAgreed = true
                failure = nil
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(PinFlow.replaceWarning)
        }
        .errorAlert($migrateFailure)
    }

    private func migrate() {
        guard let location = model.location.value else { return }
        do {
            try TerminalLauncher.open(location, arguments: ["migrate"])
        } catch {
            migrateFailure = (error as? KeryxError) ?? .terminal(error.localizedDescription)
        }
    }

    // MARK: - Saving

    /// Typing again clears what was said of the last attempt — not the fields being
    /// emptied after it, which is what says it.
    private func edited() {
        guard !first.isEmpty || !second.isEmpty else { return }
        digitsProblem = nil
        failure = nil
    }

    private func save(_ status: PinStatus, agreed: Bool = false) {
        guard !saving else { return }
        submitted = true
        let replace: Bool
        switch PinWording.saveStep(status, first: first, second: second, agreed: agreed) {
        case .wait:
            return
        case .confirm:
            confirmingReplace = true
            return
        case let .save(replacing):
            replace = replacing
        }
        let digits = first
        saving = true
        failure = nil
        digitsProblem = nil
        saveTask = Task {
            let answer = await model.setPin(digits, replace: replace)
            saveTask = nil
            saving = false
            guard !Task.isCancelled else { return }
            switch answer {
            case let .success(result):
                forget()
                saved = result
            case let .failure(error):
                // Typed again from nothing, whatever went wrong.
                forget()
                submitted = false
                let said = PinWording.failure(error)
                if said.aboutTheDigits {
                    digitsProblem = said.text
                } else {
                    failure = said.text
                    // This Mac's PIN is not what the sheet thought: read it again.
                    await model.refresh(.pin)
                }
            }
        }
    }

    /// Empties both fields: once saved, and whenever the sheet goes, saved or not.
    private func forget() {
        first = ""
        second = ""
    }

    private func close() {
        forget()
        dismiss()
    }
}

// MARK: - Opening it

/// Presents the PIN sheet, asking first — "Are you sure?", with a destructive button —
/// when it would replace a working PIN. Every way into the sheet goes through here, so
/// replacing always takes this yes, the digits typed twice, and the sheet's own "Replace
/// the PIN?".
struct PinFlow: ViewModifier {
    @Binding var isPresented: Bool

    @Environment(AppModel.self) private var model
    @State private var confirming = false
    @State private var confirmed = false
    @State private var showing = false

    func body(content: Content) -> some View {
        content
            .onChange(of: isPresented) {
                guard isPresented else { return }
                Task {
                    // Never the fields for a working PIN before the question: read it as it
                    // is now, whatever was read before.
                    await model.refresh(.pin)
                    if let status = model.pin.value, PinWording.asksFirst(status) {
                        confirming = true
                    } else {
                        confirmed = false
                        showing = true
                    }
                }
            }
            .confirmationDialog("Change the Phone PIN?", isPresented: $confirming) {
                Button("Change PIN…", role: .destructive) {
                    confirmed = true
                    showing = true
                }
                Button("Cancel", role: .cancel) { isPresented = false }
            } message: {
                Text(PinFlow.replaceWarning)
            }
            .onChange(of: confirming) {
                guard !confirming else { return }
                // Dismissed without an answer (Escape) is a no; the button's own action may
                // land after the dialog closes, so look once it has.
                Task { if !showing { isPresented = false } }
            }
            .sheet(isPresented: $showing, onDismiss: {
                confirmed = false
                isPresented = false
            }) {
                PinSheet(replaceConfirmed: confirmed)
            }
    }

    static let replaceWarning = "The PIN you use on calls stops working once the new one is saved and Keryx "
        + "restarts. You'll type the new one twice."
}

extension View {
    /// The PIN sheet, through the question a replacement asks first.
    package func pinSheet(isPresented: Binding<Bool>) -> some View {
        modifier(PinFlow(isPresented: isPresented))
    }
}

/// "Set PIN…" or "Change PIN…", opening the PIN sheet. `fallbackSet`: whether a PIN is
/// set as something else has said it, until `keryx pin` answers.
package struct PinButton: View {
    var fallbackSet = false

    @Environment(AppModel.self) private var model
    @State private var presented = false

    package init(fallbackSet: Bool = false) {
        self.fallbackSet = fallbackSet
    }

    package var body: some View {
        Button(PinWording.buttonTitle(model.pin.value, fallbackSet: fallbackSet)) { presented = true }
            .disabled(model.client == nil)
            .pinSheet(isPresented: $presented)
            .task { if model.pin.value == nil { await model.refresh(.pin) } }
    }
}
