import KeryxKit
import SwiftUI

/// Checking, then You're Ready in place. Checking is a list that ticks itself — the voice,
/// the work, the phone, the background service — and finishes here what Models started: the
/// downloads, the model chosen, the voice and model servers started (`LocalWork`). The
/// service is installed here (`service install`). A row that failed says why with Fix…,
/// which goes back to the frame that sets it, Back intact. Once the plan is ready the frame
/// becomes You're Ready: the number to ring, and what can be done whenever the owner likes.
package struct FinishFrame: View {
    let close: () -> Void
    let showMain: () -> Void

    @Environment(AppModel.self) private var model
    @Environment(SetupWizard.self) private var wizard
    @State private var installing = false
    @State private var installFailure: KeryxError?
    @State private var using = false
    @State private var useFailure: KeryxError?
    @State private var profileOpen = false
    @State private var projectsOpen = false
    /// SEAM: the agent's hello (`auth smoke`), when the client can run it.
    @State private var smoke = StreamRun<AgentSmoked>()
    @State private var smokeAsked = false

    package init(close: @escaping () -> Void, showMain: @escaping () -> Void) {
        self.close = close
        self.showMain = showMain
    }

    package var body: some View {
        let facts = WizardFacts(model)
        let ending = CheckingRules.ending(facts.plan, phoneLater: wizard.phoneLater)
        Group {
            if ending == .checking {
                checking(facts)
            } else {
                ready(facts, ending: ending)
            }
        }
        .task { await model.refresh(.setupPlan, .voice, .models, .service, .pin, .phoneWebhooks) }
        .onChange(of: nextLocal(facts), initial: true) { _, step in take(step) }
        .onChange(of: CheckingRules.serviceInstallable(facts.plan), initial: true) { _, installable in
            if installable, !wizard.serviceAsked { install() }
        }
        .onChange(of: facts.done("agents"), initial: true) { _, done in if done { smokeTest() } }
        .sheet(isPresented: $profileOpen) { ProfileSheet() }
        .sheet(isPresented: $projectsOpen) { ProjectContextSheet() }
    }

    // MARK: - Checking

    private func checking(_ facts: WizardFacts) -> some View {
        WizardPage(symbol: .system("checklist"), title: WizardWording.checkingTitle,
                   sentence: WizardWording.checkingSentence, position: wizard.position(facts)) {
            VStack(spacing: 8) {
                ForEach(CheckRow.allCases) { row in
                    rowView(row, facts: facts)
                }
            }
        } trailing: {
            Button("Back") { wizard.back() }
                .keyboardShortcut(.cancelAction)
                .disabled(wizard.path.count < 2)
            Button("Done") { close() }
                .buttonStyle(.borderedProminent)
                .disabled(true)
        }
    }

    /// One row: its state in a line, with what can be done about it.
    private func rowView(_ row: CheckRow, facts: WizardFacts) -> some View {
        let line = line(row, facts: facts)
        return HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text(WizardWording.checkRow(row)).font(.headline)
                StatusLine(tone: line.tone, text: line.text, fraction: line.fraction)
                if let failure = line.failure, failure.detail != nil {
                    DetailsDisclosure(failure.detail ?? "")
                }
            }
            Spacer(minLength: 8)
            if line.tone == .problem, let retry = line.retry {
                Button("Try Again") { retry() }
                    .controlSize(.regular)
            }
            if line.tone == .problem || line.tone == .none, row.frame != .finish {
                Button("Fix…") { wizard.go(to: row.frame) }
                    .controlSize(.regular)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quinary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    /// What a row says: a tone, a sentence, a bar while something downloads, a failure's
    /// details and its retry.
    struct Line {
        var tone: StatusLine.Tone
        var text: String
        var fraction: Double?
        var failure: StreamFailure?
        var retry: (() -> Void)?
    }

    private func line(_ row: CheckRow, facts: WizardFacts) -> Line {
        let step = facts.plan?.step(row.step)
        let next = nextLocal(facts)
        // What this Mac is still doing for the row comes first.
        if let working = localLine(row, next: next) { return working }
        switch row {
        case .phone where CheckingRules.leftForLater(step, plan: facts.plan, phoneLater: wizard.phoneLater):
            return Line(tone: .none, text: "Set up later, from the Phone section.")
        case .service where CheckingRules.leftForLater(step, plan: facts.plan, phoneLater: wizard.phoneLater):
            return Line(tone: .none, text: "Set up later, from the Overview.")
        case .service:
            return serviceLine(step, plan: facts.plan)
        case .work:
            if step?.kind == .done, let smoke = smokeLine() { return smoke }
        default:
            break
        }
        switch step?.kind {
        case .done?:
            return Line(tone: .ok, text: doneText(row, facts: facts))
        case .failed?:
            return Line(tone: .problem, text: step.flatMap(\.why).map(Wording.sentence) ?? "It isn't working.")
        case .blocked?:
            return Line(tone: .none, text: "Waits for \(waiting(step, plan: facts.plan)).")
        default:
            return Line(tone: .none, text: step.flatMap(\.why).map(Wording.sentence) ?? "Not set up yet.")
        }
    }

    private func doneText(_ row: CheckRow, facts: WizardFacts) -> String {
        switch row {
        case .voice:
            switch model.voice.value?.kind {
            case .here?: return "Speaks on this Mac."
            case .server?: return "Speaks through your voice server."
            default: return "Speaks through OpenAI."
            }
        case .work:
            return "\(Wording.agent(facts.text("AGENT_BACKEND") ?? "claude")) does the work."
        case .phone:
            return facts.text("TWILIO_NUMBER").map { "Answers on \($0)." } ?? "Connected."
        case .service:
            return WizardWording.serviceSentence
        }
    }

    /// "the phone" or "the voice and the work": what a blocked step waits for.
    private func waiting(_ step: SetupStep?, plan: SetupPlan?) -> String {
        let names = (step?.blockedBy ?? []).filter { plan?.step($0)?.kind != .done }.map { id -> String in
            switch id {
            case "phone": "the phone"
            case "voice": "the voice"
            case "agents": "the work"
            case "owner": "your details"
            default: plan?.step(id)?.title.lowercased() ?? id
            }
        }
        return names.isEmpty ? "another step" : Wording.list(names)
    }

    private func serviceLine(_ step: SetupStep?, plan: SetupPlan?) -> Line {
        if installing { return Line(tone: .working, text: "Installing Keryx's background service…") }
        if let installFailure {
            return Line(tone: .problem, text: installFailure.said("The service wasn't installed.").message,
                        failure: installFailure.said("The service wasn't installed."), retry: { install() })
        }
        switch step?.kind {
        case .done?: return Line(tone: .ok, text: WizardWording.serviceSentence)
        case .blocked?: return Line(tone: .none, text: "Installed once \(waiting(step, plan: plan)) \(step?.blockedBy.count == 1 ? "is" : "are") set up.")
        default: return Line(tone: .none, text: WizardWording.serviceSentence)
        }
    }

    // MARK: - This Mac's work

    private func nextLocal(_ facts: WizardFacts) -> LocalStep {
        let intent = LocalIntent.of(wizard, facts: facts, voice: model.voice.value, models: model.models.value)
        let busy = model.downloads.isBusy || model.voiceDownloadRun.isBusy || model.modelServerRun.isBusy
            || model.voiceSetup.isBusy || using
        return LocalWork.next(intent, models: model.models.value, voice: model.voice.value, facts: facts, busy: busy)
    }

    /// A row while this Mac works on it, or when that work failed.
    private func localLine(_ row: CheckRow, next: LocalStep) -> Line? {
        switch row {
        case .voice:
            if case let .running(activity)? = model.voiceDownloadRun.phase {
                let sizes = activity.progress.flatMap { RunSteps.sizes(done: $0.done, total: $0.total) }
                return Line(tone: .working, text: "Downloading the speech models" + (sizes.map { " · \($0)" } ?? ""),
                            fraction: activity.progress?.fraction)
            }
            if let failure = model.voiceDownloadRun.failure {
                return Line(tone: .problem, text: failure.message, failure: failure,
                            retry: { model.voiceDownloadRun.dismiss(); model.downloadVoiceModels() })
            }
            if case let .running(_, message, _)? = model.voiceSetup.phase {
                return Line(tone: .working, text: message)
            }
            if model.thisMacSetup?.startsServer == true, case let .running(activity)? = model.modelServerRun.phase {
                return Line(tone: .working, text: activity.message)
            }
            if case let .failed(failure)? = model.voiceSetup.phase {
                return Line(tone: .problem, text: failure.message, failure: failure,
                            retry: { model.dismissThisMacSetup(); model.setUpThisMac() })
            }
            // The model server this setup began with did not start.
            if model.thisMacSetup?.startsServer == true, let failure = model.modelServerRun.failure {
                return Line(tone: .problem, text: failure.message, failure: failure,
                            retry: { model.dismissThisMacSetup(); model.setUpThisMac(startServer: true) })
            }
        case .work:
            if let current = model.downloads.current {
                let title = model.models.value?.models.first { $0.name == current.name }?.title ?? current.name
                switch current.phase {
                case let .running(done, total):
                    return Line(tone: .working,
                                text: "Downloading \(title) · \(RunSteps.sizes(done: done, total: total) ?? "")",
                                fraction: total > 0 ? Double(done) / Double(total) : nil)
                case let .failed(failure):
                    return Line(tone: .problem, text: failure.message, failure: failure,
                                retry: { model.download(current.name) })
                case .cancelling, .finished:
                    break
                }
            }
            if using { return Line(tone: .working, text: "Choosing the model…") }
            if let useFailure {
                let failure = useFailure.said("The model wasn't chosen.")
                return Line(tone: .problem, text: failure.message, failure: failure, retry: {
                    self.useFailure = nil
                    take(next)
                })
            }
            if model.thisMacSetup?.startsServer != true, case let .running(activity)? = model.modelServerRun.phase {
                return Line(tone: .working, text: activity.message)
            }
            if model.thisMacSetup?.startsServer != true, let failure = model.modelServerRun.failure {
                return Line(tone: .problem, text: failure.message, failure: failure,
                            retry: { model.modelServerRun.dismiss(); model.runModelServer() })
            }
        case .phone, .service:
            break
        }
        // A step still to take for this row: it starts as soon as nothing else runs.
        if next != .done, next != .wait, next.row == row {
            return Line(tone: .working, text: "Getting ready on this Mac…")
        }
        return nil
    }

    /// Takes the next step on this Mac, unless the one it would repeat has failed.
    private func take(_ step: LocalStep) {
        switch step {
        case let .download(name):
            if case .failed? = model.downloads.active[name] { return }
            model.download(name)
        case let .use(name, defaultAgent):
            guard useFailure == nil, !using else { return }
            using = true
            Task {
                defer { using = false }
                if let error = await model.use(model: name, defaultAgent: defaultAgent) { useFailure = error }
                await model.refresh(.models, .config, .setupPlan)
            }
        case .downloadSpeech:
            guard model.voiceDownloadRun.failure == nil else { return }
            model.downloadVoiceModels()
        case .setUpVoice:
            if case .failed? = model.voiceSetup.phase { return }
            if model.thisMacSetup?.startsServer == true, model.modelServerRun.failure != nil { return }
            model.setUpThisMac()
        case .startServer:
            guard model.modelServerRun.failure == nil else { return }
            model.runModelServer()
        case .wait, .done:
            break
        }
    }

    private func install() {
        guard !installing else { return }
        wizard.serviceAsked = true
        installing = true
        installFailure = nil
        Task {
            defer { installing = false }
            if case let .failure(error) = await model.installService() { installFailure = error }
        }
    }

    // MARK: - The agent's hello (SEAM)

    private func smokeTest() {
        guard !smokeAsked, let client = model.client as? AgentSmokeTesting,
              let agent = WizardFacts(model).text("AGENT_BACKEND") else { return }
        smokeAsked = true
        smoke.start(reason: "Asking the agent to say hello", { client.smokeTest(agent) })
    }

    private func smokeLine() -> Line? {
        let name = Wording.agent(WizardFacts(model).text("AGENT_BACKEND") ?? "claude")
        switch smoke.phase {
        case .running?, .cancelling?: return Line(tone: .working, text: "Asking \(name) to say hello…")
        case let .finished(done)?:
            return Line(tone: done.ok ? .ok : .problem, text: done.summary,
                        failure: done.ok ? nil : StreamFailure(done.summary, detail: done.detail),
                        retry: done.ok ? nil : { smoke.dismiss(); smokeAsked = false; smokeTest() })
        case let .failed(failure)?:
            return Line(tone: .problem, text: failure.message, failure: failure,
                        retry: { smoke.dismiss(); smokeAsked = false; smokeTest() })
        case nil: return nil
        }
    }

    // MARK: - You're Ready

    private func ready(_ facts: WizardFacts, ending: WizardEnding) -> some View {
        let who = WizardWording.assistant(facts, status: model.status.value)
        let nearly = ending == .nearlyReady
        let sentence = nearly
            ? WizardWording.nearlyReady(serviceWaits: facts.plan?.step("service")?.kind != .done)
            : WizardWording.ready(who, number: facts.text("TWILIO_NUMBER"), pinSet: model.pin.value?.set ?? true)
        return WizardPage(symbol: .system(nearly ? "checkmark.seal" : "checkmark.seal.fill", nearly ? .orange : .green),
                          title: nearly ? WizardWording.nearlyTitle : WizardWording.readyTitle,
                          sentence: sentence, position: wizard.position(facts)) {
            VStack(alignment: .leading, spacing: 8) {
                if !nearly, model.phoneWebhooks.value?.matches == false {
                    StatusLine(tone: .warning, text: WizardWording.pointLater)
                }
                let later = (facts.plan?.remaining ?? []).filter {
                    CheckingRules.leftForLater($0, plan: facts.plan, phoneLater: wizard.phoneLater)
                }
                if nearly, !later.isEmpty {
                    StatusLine(tone: .none, text: "Left for later: \(Wording.list(later.map { Wording.titleCase($0.title) })).")
                }
                Text(WizardWording.whenever)
                    .font(.headline)
                    .padding(.top, 4)
                VStack(spacing: 0) {
                    extra("Connect Google", "envelope", step: "google", facts) { open(.connect(integration: "google")) }
                    Divider().padding(.leading, 40)
                    extra("Turn On Plugins", "puzzlepiece.extension", step: "plugins", facts) { open(.show(.plugins)) }
                    Divider().padding(.leading, 40)
                    extra("Tell \(who) About Yourself", "person.text.rectangle", step: "profile", facts) {
                        profileOpen = true
                    }
                    Divider().padding(.leading, 40)
                    extra("Brief \(who) on Your Projects", "folder", step: "projects", facts) { projectsOpen = true }
                    Divider().padding(.leading, 40)
                    extra("Let Keryx File Its Own Bugs", "ladybug", step: "issues", facts) {
                        open(.connect(integration: "github"))
                    }
                }
                .background(.quinary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
        } trailing: {
            Button("Back") { wizard.back() }
                .keyboardShortcut(.cancelAction)
            Button("Done") { close() }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
        }
    }

    /// One thing to do whenever: its title, a tick when its step is done, and the button.
    private func extra(_ title: String, _ symbol: String, step: String, _ facts: WizardFacts,
                       action: @escaping () -> Void) -> some View {
        let done = facts.done(step)
        return HStack(spacing: 12) {
            Image(systemName: symbol)
                .foregroundStyle(.tint)
                .frame(width: 20)
            Text(title)
            if done {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                    .accessibilityLabel("Done")
            }
            Spacer()
            Button(done ? "Open…" : "Set Up…") { action() }
                .controlSize(.regular)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
    }

    /// Opens the main window at `request` and closes the wizard.
    private func open(_ request: AppRequest) {
        model.request(request)
        showMain()
        close()
    }
}
