import KeryxKit
import SwiftUI

/// Your PIN: chosen through the PIN sheet (`PinFlow`, which types it twice and asks before a
/// working PIN is replaced), or left to the first call (`setup mark pin`). A PIN the app
/// cannot change — in the environment, or still where Jarvis kept it — is said, and only
/// Continue is offered.
package struct PinFrame: View {
    @Environment(AppModel.self) private var model
    @Environment(SetupWizard.self) private var wizard
    @State private var choosing = false
    @State private var marking = false
    @State private var failure: KeryxError?

    package init() {}

    package var body: some View {
        let facts = WizardFacts(model)
        let who = WizardWording.assistant(facts, status: model.status.value)
        let status = model.pin.value
        let kind = status.map(PinWording.kind)
        WizardPage(symbol: .system(status?.set == true ? "lock.fill" : "lock"), title: WizardWording.pinTitle,
                   sentence: WizardWording.pinSentence(who), position: wizard.position(facts)) {
            VStack(spacing: 12) {
                if let status {
                    section(status, assistant: who)
                } else if let error = model.pin.error {
                    WizardSection(title: "Phone PIN") { ErrorText(error.said("Keryx couldn't say whether a PIN is set.")) }
                } else {
                    WizardSection(title: "Phone PIN") { StatusLine(tone: .working, text: "Checking the PIN…") }
                }
                if let failure {
                    ErrorText(failure.said("That wasn't recorded."))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        } trailing: {
            if marking { ProgressView().controlSize(.small) }
            Button("Back") { wizard.back() }
                .keyboardShortcut(.cancelAction)
            if let status, PinWording.canSet(status), !status.set {
                Button(WizardWording.pinLater) { decideLater() }
                    .disabled(marking)
                Button("Choose PIN…") { choosing = true }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
            } else {
                Button("Continue") { Task { await wizard.moveOn(model) } }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(status == nil || kind == .environment(usable: false))
            }
        }
        .pinSheet(isPresented: $choosing)
        .task { await model.refresh(.pin) }
    }

    private func section(_ status: PinStatus, assistant: String) -> some View {
        let banner = PinWording.banner(status, assistant: assistant)
        let set = status.set && PinWording.kind(status) == .set
        return WizardSection(title: "Phone PIN", state: set ? .done : .open) {
            VStack(alignment: .leading, spacing: 8) {
                Label {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(banner.title).font(.callout.weight(.semibold))
                        Text(status.set || !PinWording.canSet(status) ? banner.text : WizardWording.pinCaption)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                } icon: {
                    Image(systemName: banner.symbol)
                        .foregroundStyle(banner.tone == .ok ? Color.green : banner.tone == .blocked ? .red : .orange)
                }
                if let lock = PinWording.lockLine(status) {
                    StatusLine(tone: .warning, text: lock)
                }
                if let terminal = banner.terminal {
                    DetailsDisclosure(terminal, heading: "For the Terminal")
                }
                if set {
                    Button("Change PIN…") { choosing = true }
                        .controlSize(.regular)
                }
            }
        }
    }

    /// Leaves the PIN to the first call, which asks the caller to choose one.
    private func decideLater() {
        marking = true
        failure = nil
        Task {
            defer { marking = false }
            if let error = await WizardWrite.apply([.mark("pin")], with: model) {
                failure = error
                return
            }
            await wizard.moveOn(model)
        }
    }
}
