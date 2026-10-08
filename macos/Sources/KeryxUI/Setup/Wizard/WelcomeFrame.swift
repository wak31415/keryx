import KeryxKit
import SwiftUI

/// Welcome to Keryx: what Keryx does and how far there is to go, and the way in. Writes
/// nothing; Not Now closes the window for this launch.
package struct WelcomeFrame: View {
    let close: () -> Void

    @Environment(AppModel.self) private var model
    @Environment(SetupWizard.self) private var wizard
    @AppStorage(SetupLaunch.hiddenKey) private var hiddenAtLaunch = false

    package init(close: @escaping () -> Void) {
        self.close = close
    }

    package var body: some View {
        let facts = WizardFacts(model)
        let resuming = WizardFlow.resuming(mode: wizard.mode, facts: facts)
        let frames = WizardFlow.questions(mode: wizard.mode, facts: facts)
        let sentence = WizardWording.welcome(mode: wizard.mode, resuming: resuming,
                                             left: facts.plan?.remaining.count ?? 0, frames: frames,
                                             assistant: WizardWording.assistant(facts, status: model.status.value))
        WizardPage(symbol: .mark, title: WizardWording.welcomeTitle, sentence: sentence, position: nil, hero: true) {
            if wizard.mode == .firstRun, !resuming {
                Text(WizardWording.welcomeNeeds)
                    .font(.callout)
                    .foregroundStyle(.tertiary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 420)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } leading: {
            if wizard.mode == .firstRun {
                Toggle("Don't show this again", isOn: $hiddenAtLaunch)
                    .toggleStyle(.checkbox)
                    .controlSize(.regular)
                    .foregroundStyle(.secondary)
            }
        } trailing: {
            Button("Not Now") { close() }
                .keyboardShortcut(.cancelAction)
            Button(WizardWording.welcomeButton(resuming: resuming)) { wizard.advance(facts) }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
        }
    }
}
