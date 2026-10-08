import KeryxKit
import SwiftUI

/// Checking, then You're Ready in place. Checking lists the voice, the work, the phone and
/// answering calls, then every other step the plan still holds open — nothing it waits
/// for is ever off the list — each with Fix… to the frame that sets it. It finishes what
/// Models chose for this Mac (`LocalWork`): in a first run by itself, step after step; in a
/// review only after a click. What the owner stops (Cancel on a row, or elsewhere) is never
/// started again by itself, only by Start Again. Leaving is always one click: Close while
/// anything is left. Once the plan is ready the frame becomes You're Ready — or Nearly Ready,
/// when the phone was left for later or the number still rings elsewhere.
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

    package init(close: @escaping () -> Void, showMain: @escaping () -> Void) {
        self.close = close
        self.showMain = showMain
    }

    package var body: some View {
        let facts = WizardFacts(model)
        let ending = CheckingRules.ending(facts.plan, phoneLater: wizard.phoneLater,
                                          pointed: model.phoneWebhooks.value.map { $0.error == nil ? $0.matches : true })
        Group {
            if ending == .checking {
                checking(facts)
            } else {
                ready(facts, ending: ending)
            }
        }
        .task { await model.refresh(.setupPlan, .voice, .models, .service, .pin, .phoneWebhooks, .config) }
        .onChange(of: nextLocal(facts), initial: true) { _, step in takeByItself(step) }
        .onChange(of: CheckingRules.serviceInstallable(facts.plan), initial: true) { _, installable in
            if installable, wizard.mode == .firstRun, !wizard.serviceAsked { install() }
        }
        .onChange(of: facts.done("agents"), initial: true) { _, done in
            // A paid run: by itself only in a first run; a review offers it as a click.
            if done, wizard.mode == .firstRun, let agent = facts.text("AGENT_BACKEND") { model.smokeTest(agent) }
        }
        .onChange(of: ending, initial: true) { _, ending in
            // Seen through: from now on a problem is the checklist's, not a window's.
            if CheckingRules.completes(ending), wizard.recordsCompletion { SetupLaunch.markCompleted() }
        }
        .sheet(isPresented: $profileOpen) { ProfileSheet() }
        .sheet(isPresented: $projectsOpen) { ProjectContextSheet() }
    }

    // MARK: - Checking

    private func checking(_ facts: WizardFacts) -> some View {
        let intent = LocalIntent.of(wizard, facts: facts, voice: model.voice.value, models: model.models.value)
        return WizardPage(symbol: .system("checklist"), title: WizardWording.checkingTitle,
                          sentence: WizardWording.checkingSentence, position: wizard.position(facts)) {
            ScrollView {
                VStack(spacing: 8) {
                    ForEach(CheckRow.rows(facts.plan, local: !intent.isEmpty)) { row in
                        rowView(row, facts: facts)
                    }
                }
            }
            .scrollBounceBehavior(.basedOnSize)
        } trailing: {
            Button("Back") { wizard.back() }
                .keyboardShortcut(.cancelAction)
                .disabled(wizard.path.count < 2)
            Button("Close") { close() }
            .buttonStyle(.borderedProminent)
            .keyboardShortcut(.defaultAction)
        }
    }

    /// One row: its state in a line, with what can be done about it.
    private func rowView(_ row: CheckRow, facts: WizardFacts) -> some View {
        let line = line(row, facts: facts)
        return HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text(WizardWording.checkRow(row, plan: facts.plan)).font(.headline)
                StatusLine(tone: line.tone, text: line.text, fraction: line.fraction)
                if let failure = line.failure, let detail = failure.detail {
                    DetailsDisclosure(detail)
                }
            }
            Spacer(minLength: 8)
            if let cancel = line.cancel {
                Button("Cancel") { cancel() }
                    .controlSize(.regular)
            }
            if let action = line.action {
                Button(action.title) { action.run() }
                    .controlSize(.regular)
            }
            if line.tone == .problem || line.tone == .none, line.cancel == nil {
                if let frame = row.frame, frame != .finish {
                    Button("Fix…") { wizard.go(to: frame) }
                        .controlSize(.regular)
                } else if row.frame == nil, case .step = row {
                    Button("Fix…") { open(.show(.overview)) }
                        .controlSize(.regular)
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quinary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    /// What a row says: a tone, a sentence, a bar while something downloads, a failure's
    /// details, a Cancel while it works, and a button of its own (Try Again, Start Again, a
    /// review's click to go on).
    struct Line {
        var tone: StatusLine.Tone
        var text: String
        var fraction: Double?
        var failure: StreamFailure?
        var cancel: (() -> Void)?
        var action: (title: String, run: () -> Void)?
    }

    private func line(_ row: CheckRow, facts: WizardFacts) -> Line {
        let step = facts.plan?.step(row.step)
        let titles = Wording.titles(model.config.value)
        let next = nextLocal(facts)
        let intent = LocalIntent.of(wizard, facts: facts, voice: model.voice.value, models: model.models.value)
        // What this Mac is doing (or was stopped from doing) for the row comes first.
        if let working = localLine(row, next: next, intent: intent) { return working }
        if case .phone = row, CheckingRules.leftForLater(step, plan: facts.plan, phoneLater: wizard.phoneLater) {
            return Line(tone: .none, text: "Set up later, from the Phone section.")
        }
        if case .service = row {
            if CheckingRules.leftForLater(step, plan: facts.plan, phoneLater: wizard.phoneLater) {
                return Line(tone: .none, text: "Set up later, from the Overview.")
            }
            return serviceLine(step, plan: facts.plan)
        }
        if case .work = row, step?.kind == .done {
            if let smoke = smokeLine() { return smoke }
            // A review runs the agent's (paid) test task only on a click.
            if wizard.mode == .review, model.client is AgentSmokeTesting, let agent = facts.text("AGENT_BACKEND") {
                return Line(tone: .ok, text: doneText(row, facts: facts),
                            action: ("Run a Test", { model.smokeTest(agent) }))
            }
        }
        switch step?.kind {
        case .done?:
            return Line(tone: .ok, text: doneText(row, facts: facts))
        case .failed?:
            return Line(tone: .problem, text: WizardWording.why(step, titles: titles) ?? "It isn't working.")
        case .blocked?:
            return Line(tone: .none, text: "After \(waiting(step, plan: facts.plan)) \(waitVerb(step, plan: facts.plan)) ready.")
        default:
            return Line(tone: .none, text: WizardWording.why(step, titles: titles) ?? "Not set up yet.")
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
            return facts.text("TWILIO_NUMBER").map { "Answers on \(PhoneDisplay.format($0))." } ?? "Connected."
        case .service:
            return WizardWording.serviceSentence(WizardWording.assistant(facts, status: model.status.value))
        case .step:
            return "Done."
        }
    }

    /// "the phone" or "the voice and the work": what a blocked step waits for.
    private func waiting(_ step: SetupStep?, plan: SetupPlan?) -> String {
        let names = waitingFor(step, plan: plan).map { id -> String in
            switch id {
            case "phone": "the phone"
            case "voice": "the voice"
            case "agents": "the work"
            case "owner": "your details"
            case "migrate": "the move"
            default: plan?.step(id)?.title.lowercased() ?? id
            }
        }
        return names.isEmpty ? "another step" : Wording.list(names)
    }

    private func waitingFor(_ step: SetupStep?, plan: SetupPlan?) -> [String] {
        (step?.blockedBy ?? []).filter { plan?.step($0)?.kind != .done }
    }

    private func waitVerb(_ step: SetupStep?, plan: SetupPlan?) -> String {
        waitingFor(step, plan: plan).count > 1 ? "are" : "is"
    }

    private func serviceLine(_ step: SetupStep?, plan: SetupPlan?) -> Line {
        let sentence = WizardWording.serviceSentence(WizardWording.assistant(WizardFacts(model), status: model.status.value))
        if installing || model.serviceChange == .installing { return Line(tone: .working, text: "Starting Keryx…") }
        if let installFailure {
            let failure = installFailure.said("Keryx couldn't start answering calls.")
            return Line(tone: .problem, text: failure.message, failure: failure,
                        action: ("Try Again", { install() }))
        }
        switch step?.kind {
        case .done?:
            return Line(tone: .ok, text: sentence)
        case .blocked?:
            return Line(tone: .none, text: "After \(waiting(step, plan: plan)) \(waitVerb(step, plan: plan)) ready.")
        default:
            // In a review, or once asked: only on a click.
            if wizard.mode == .review || wizard.serviceAsked {
                return Line(tone: .none, text: "Keryx isn't answering calls yet.",
                            action: ("Start Keryx", { install() }))
            }
            return Line(tone: .none, text: sentence)
        }
    }

    // MARK: - This Mac's work

    private func nextLocal(_ facts: WizardFacts) -> LocalStep {
        let intent = LocalIntent.of(wizard, facts: facts, voice: model.voice.value, models: model.models.value)
        let busy = model.downloads.isBusy || model.voiceDownloadRun.isBusy || model.modelServerRun.isBusy
            || model.voiceSetup.isBusy || model.voiceLLMBusy || using
        return LocalWork.next(intent, models: model.models.value, voice: model.voice.value, facts: facts, busy: busy)
    }

    /// A row while this Mac works on it, when that work failed or was stopped, or when a
    /// review is waiting for the click that lets it go on.
    private func localLine(_ row: CheckRow, next: LocalStep, intent: LocalIntent) -> Line? {
        switch row {
        case .voice:
            // Stopped part way, the CLI puts back what it was replacing: said, never a hang.
            if model.voiceLLMRun.phase == .cancelling { return Line(tone: .working, text: VoiceLLMWording.stopping) }
            if case let .running(activity)? = model.voiceLLMRun.phase {
                let sizes = activity.progress.flatMap { RunSteps.sizes(done: $0.done, total: $0.total) }
                return Line(tone: .working, text: VoiceWording.running(activity.message) + (sizes.map { " · \($0)" } ?? ""),
                            fraction: activity.progress?.fraction, cancel: { stop(voiceLLMStep) })
            }
            if let failure = model.voiceLLMRun.failure {
                if LocalRefusal.of(failure) == .liveCall {
                    return Line(tone: .none, text: VoiceLLMWording.onACall(assistant),
                                action: ("Try Again", { model.voiceLLMRun.dismiss(); take(next) }))
                }
                return Line(tone: .problem, text: failure.message, failure: failure,
                            action: ("Try Again", { model.voiceLLMRun.dismiss(); take(next) }))
            }
            if case let .running(activity)? = model.voiceDownloadRun.phase {
                let sizes = activity.progress.flatMap { RunSteps.sizes(done: $0.done, total: $0.total) }
                return Line(tone: .working, text: "Downloading the speech models" + (sizes.map { " · \($0)" } ?? ""),
                            fraction: activity.progress?.fraction, cancel: { stop(.downloadSpeech) })
            }
            if let failure = model.voiceDownloadRun.failure {
                return Line(tone: .problem, text: failure.message, failure: failure,
                            action: ("Try Again", { model.voiceDownloadRun.dismiss(); model.downloadVoiceModels() }))
            }
            if case let .running(_, message, _)? = model.voiceSetup.phase {
                return Line(tone: .working, text: message, cancel: { stop(.setUpVoice) })
            }
            if model.thisMacSetup?.startsServer == true, case let .running(activity)? = model.modelServerRun.phase {
                return Line(tone: .working, text: activity.message, cancel: { stop(.setUpVoice) })
            }
            if case let .failed(failure)? = model.voiceSetup.phase {
                // A call in progress is a wait, not a failure.
                if LocalRefusal.of(failure) == .liveCall {
                    return Line(tone: .none, text: VoiceLLMWording.onACall(assistant),
                                action: ("Try Again", { model.dismissThisMacSetup(); model.setUpThisMac() }))
                }
                return Line(tone: .problem, text: failure.message, failure: failure,
                            action: ("Try Again", { model.dismissThisMacSetup(); model.setUpThisMac() }))
            }
            if model.thisMacSetup?.startsServer == true, let failure = model.modelServerRun.failure {
                return Line(tone: .problem, text: failure.message, failure: failure,
                            action: ("Try Again", { model.dismissThisMacSetup(); model.setUpThisMac(startServer: true) }))
            }
        case .work:
            if model.thisMacSetup?.startsServer != true, case let .running(activity)? = model.modelServerRun.phase {
                return Line(tone: .working, text: activity.message, cancel: { stop(.startServer) })
            }
            if model.thisMacSetup?.startsServer != true, let failure = model.modelServerRun.failure {
                return Line(tone: .problem, text: failure.message, failure: failure,
                            action: ("Try Again", { model.modelServerRun.dismiss(); model.runModelServer() }))
            }
            if using { return Line(tone: .working, text: "Choosing the model…") }
            if let useFailure {
                let failure = useFailure.said("The model wasn't chosen.")
                return Line(tone: .problem, text: failure.message, failure: failure, action: ("Try Again", {
                    self.useFailure = nil
                    take(next)
                }))
            }
        case .phone, .service, .step:
            break
        }
        // A model download belongs to whichever row its model is for.
        if case let .download(name)? = downloading, LocalStep.download(name).row(intent) == row {
            return downloadLine(name)
        }
        if let stopped = CheckingRules.stopped(row, next: next, intent: intent, stopped: wizard.stopped) {
            return Line(tone: .none, text: "Stopped.", action: ("Start Again", {
                wizard.stopped.remove(stopped)
                take(stopped)
            }))
        }
        // A step still to take for this row: by itself in a first run, on a click in a review.
        if next != .done, next != .wait, next.row(intent) == row {
            if wizard.mode == .review, !wizard.localApproved {
                return Line(tone: .none, text: "Not finished on this Mac yet.", action: ("Finish Setting Up", {
                    wizard.localApproved = true
                    take(next)
                }))
            }
            return Line(tone: .working, text: "Getting ready on this Mac…")
        }
        return nil
    }

    /// The assistant's name, as the run's lines say it.
    private var assistant: String? { Wording.assistant(model.status.value) }

    /// The voice LLM step the run under way is, for its Cancel.
    private var voiceLLMStep: LocalStep {
        switch model.voiceLLMChoice {
        case let .here(name)?: .voiceLLM(name)
        default: .voiceLLMShared
        }
    }

    /// The model download under way (or that failed), as the step it is.
    private var downloading: LocalStep? {
        guard let current = model.downloads.current else { return nil }
        switch current.phase {
        case .running, .failed, .cancelling: return .download(current.name)
        case .finished: return nil
        }
    }

    private func downloadLine(_ name: String) -> Line {
        let title = model.models.value?.models.first { $0.name == name }?.title ?? name
        switch model.downloads.active[name] {
        case let .running(done, total)?:
            return Line(tone: .working, text: "Downloading \(title) · \(RunSteps.sizes(done: done, total: total) ?? "")",
                        fraction: total > 0 ? Double(done) / Double(total) : nil, cancel: { stop(.download(name)) })
        case let .failed(failure)?:
            return Line(tone: .problem, text: failure.message, failure: failure,
                        action: ("Try Again", { model.download(name) }))
        default:
            return Line(tone: .working, text: "Stopping…")
        }
    }

    /// Cancel on a row: the run is stopped, and remembered so nothing starts it again by itself.
    private func stop(_ step: LocalStep) {
        wizard.stopped.insert(step)
        switch step {
        case let .download(name): model.downloads.cancel(name)
        case .downloadSpeech: model.voiceDownloadRun.cancel()
        case .voiceLLM, .voiceLLMShared: model.voiceLLMRun.cancel()
        case .setUpVoice: model.cancelThisMacSetup()
        case .startServer: model.cancelModelServer()
        case .use, .wait, .done: break
        }
    }

    /// The next step, when it may start by itself: a first run's, or a review's once the
    /// owner clicked — never one the owner stopped.
    private func takeByItself(_ step: LocalStep) {
        guard CheckingRules.startsByItself(step, mode: wizard.mode, approved: wizard.localApproved,
                                           stopped: wizard.stopped) else { return }
        take(step)
    }

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
        case let .voiceLLM(name):
            guard model.voiceLLMRun.failure == nil else { return }
            model.chooseVoiceLLM(.here(model: name))
        case .voiceLLMShared:
            guard model.voiceLLMRun.failure == nil else { return }
            model.chooseVoiceLLM(.shared)
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
            if case let .failure(error) = await model.startAnswering() { installFailure = error }
        }
    }

    // MARK: - The agent's test (SEAM)

    private func smokeLine() -> Line? {
        let name = Wording.agent(WizardFacts(model).text("AGENT_BACKEND") ?? "claude")
        let again: (title: String, run: () -> Void) = ("Try Again", {
            model.smokeRun.dismiss()
            if let agent = WizardFacts(model).text("AGENT_BACKEND") { model.smokeTest(agent) }
        })
        switch model.smokeRun.phase {
        case .running?, .cancelling?:
            return Line(tone: .working, text: "Asking \(name) to run a test task…", cancel: { model.smokeRun.cancel() })
        case let .finished(done)?:
            return Line(tone: done.ok ? .ok : .problem, text: done.summary,
                        failure: done.ok ? nil : StreamFailure(done.summary, detail: done.detail),
                        action: done.ok ? nil : again)
        case let .failed(failure)?:
            return Line(tone: .problem, text: failure.message, failure: failure, action: again)
        case nil:
            return nil
        }
    }

    // MARK: - You're Ready

    private func ready(_ facts: WizardFacts, ending: WizardEnding) -> some View {
        let who = WizardWording.assistant(facts, status: model.status.value)
        let nearly = ending != .ready
        let number = facts.text("TWILIO_NUMBER")
        let sentence = switch ending {
        case .notPointed: WizardWording.notPointed(number: number)
        case .nearlyReady: WizardWording.nearlyReady(serviceWaits: facts.plan?.step("service")?.kind != .done)
        default: WizardWording.ready(who, number: number, pinSet: model.pin.value?.set ?? true)
        }
        return WizardPage(symbol: .system(nearly ? "checkmark.seal" : "checkmark.seal.fill", nearly ? .orange : .green),
                          title: nearly ? WizardWording.nearlyTitle : WizardWording.readyTitle,
                          sentence: sentence, position: wizard.position(facts)) {
            VStack(alignment: .leading, spacing: 8) {
                let later = (facts.plan?.remaining ?? []).filter {
                    CheckingRules.leftForLater($0, plan: facts.plan, phoneLater: wizard.phoneLater)
                }
                if ending == .nearlyReady, !later.isEmpty {
                    StatusLine(tone: .none, text: "Left for later: \(Wording.list(later.map { Wording.titleCase($0.title) })).")
                }
                Text(WizardWording.whenever)
                    .font(.headline)
                    .padding(.top, 4)
                VStack(spacing: 0) {
                    extra("Connect Google", "envelope", step: "google", facts) { open(.connect(integration: "google")) }
                    Divider().padding(.leading, 64)
                    extra("Turn On Plugins", "puzzlepiece.extension", step: "plugins", facts) { open(.show(.plugins)) }
                    Divider().padding(.leading, 64)
                    extra("Tell \(who) About Yourself", "person.text.rectangle", step: "profile", facts) {
                        profileOpen = true
                    }
                    Divider().padding(.leading, 64)
                    extra("Brief \(who) on Your Projects", "folder", step: "projects", facts) { projectsOpen = true }
                    Divider().padding(.leading, 64)
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

    /// One thing to do whenever: a tick in its own column when its step is done, its symbol,
    /// its title, and the button.
    private func extra(_ title: String, _ symbol: String, step: String, _ facts: WizardFacts,
                       action: @escaping () -> Void) -> some View {
        let done = facts.done(step)
        return HStack(spacing: 10) {
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
                .opacity(done ? 1 : 0)
                .frame(width: 16)
                .accessibilityLabel("Done")
                .accessibilityHidden(!done)
            Image(systemName: symbol)
                .foregroundStyle(.tint)
                .frame(width: 20)
            Text(title)
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
