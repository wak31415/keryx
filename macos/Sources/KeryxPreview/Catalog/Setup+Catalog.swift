import Foundation
import KeryxKit
import KeryxUI
import SwiftUI

extension ScreenCatalog {
    /// The setup wizard, frame by frame: Welcome fresh, resuming and in review; Move Your
    /// Files with its plan, a conflict, the move under way and failed; About You empty, typed
    /// wrong, part set and in review; Models in every branch of each section (OpenAI, this
    /// Mac with today's shared words and with the voice's own model the CLI will offer,
    /// elsewhere; Claude, a model here downloading, a server elsewhere), answered and folded;
    /// the PIN to choose, set and in the environment; the phone from nothing to pointing it,
    /// and Twilio refusing; Checking at work and failed; You're Ready, without a PIN, and
    /// Nearly Ready; the two sheets of You're Ready's extras; the plan unread and Keryx
    /// not found.
    @MainActor static var setupEntries: [Entry] {
        let size = WizardMetrics.size
        let sheet = CGSize(width: 540, height: 600)
        return [
            // Welcome
            Entry("wizard-welcome", size: size) { wizard(.welcome, model: fresh()) },
            Entry("wizard-welcome-resuming", size: size) { wizard(.welcome, model: partway()) },
            Entry("wizard-welcome-review", size: size) { wizard(.welcome, mode: .review, model: ready()) },
            Entry("wizard-loading", size: size) {
                let model = fresh()
                model.setupPlan = Loadable(isLoading: true)
                return SetupWizardView(wizard: SetupWizard(), reads: false).environment(model)
            },
            Entry("wizard-needs-updating", size: size) {
                wizard(.welcome, model: AppModel.preview(client: PreviewClient(setupPlan: .failure(.failed(
                    command: "keryx setup plan", exitCode: 2, message: "No such command 'plan'.")))))
            },
            Entry("wizard-plan-failed", size: size) {
                wizard(.welcome, model: AppModel.preview(client: PreviewClient(setupPlan: .failure(
                    .timedOut(command: "keryx setup plan")))))
            },
            Entry("wizard-not-found", size: size) {
                wizard(.welcome, model: AppModel.preview(location: .failure(LocateFailure(lookedAt: []))))
            },
            // Move Your Files
            Entry("wizard-move-files", size: size) { wizard(.migrate, model: migrating()) },
            Entry("wizard-move-files-conflict", size: size) {
                wizard(.migrate, model: migrating(plan: .success(PreviewData.migrationPlanConflict)))
            },
            Entry("wizard-move-files-moving", size: size) {
                wizard(.migrate, model: {
                    let model = migrating(run: .holding([.step(step: "move", message: "Moving Keryx's files.")]))
                    model.migrate()
                    return model
                }())
            },
            Entry("wizard-move-files-failed", size: size) {
                wizard(.migrate, model: {
                    let model = migrating(run: .events([
                        .step(step: "move", message: "Moving Keryx's files."),
                        .error(StreamFailure("The migration stopped before it finished. Migrating again picks up "
                            + "where it stopped.", detail: "OSError: [Errno 28] No space left on device")),
                    ]))
                    model.migrate()
                    return model
                }())
            },
            // About You
            Entry("wizard-about-you", size: size) { wizard(.aboutYou, model: fresh()) },
            Entry("wizard-about-you-typed", size: size) {
                frame(.aboutYou, model: fresh()) {
                    AboutYouFrame(typed: AboutYouAnswers(name: "Ada", assistant: "",
                                                         numbers: ["+44 7700 900123", "07700 900456"]),
                                  pin: "482915")
                }
            },
            Entry("wizard-about-you-pin-set", size: size) {
                wizard(.aboutYou, model: fresh(values: ["OWNER_NAME": .string("Ada")], pin: PreviewData.pinFile))
            },
            Entry("wizard-about-you-pin-started-with", size: size) {
                wizard(.aboutYou, model: fresh(pin: PreviewData.pinEnvironment))
            },
            Entry("wizard-about-you-partway", size: size) {
                wizard(.aboutYou, model: fresh(values: ["OWNER_NAME": .string("Ada")]))
            },
            Entry("wizard-about-you-review", size: size) {
                wizard(.aboutYou, mode: .review, model: ready())
            },
            // Models
            Entry("wizard-models", size: size) { wizard(.models, model: fresh()) },
            Entry("wizard-models-answered", size: size) {
                wizard(.models, model: fresh(values: ["OPENAI_API_KEY": .string("(set)")], claudeSignedIn: true))
            },
            Entry("wizard-models-folded", size: size) {
                wizard(.models, model: fresh(plan: done(PreviewData.setupPlan, "voice"),
                                             values: ["OPENAI_API_KEY": .string("(set)")]))
            },
            Entry("wizard-models-this-mac-voice", size: size) {
                wizard(.models, model: fresh(claudeSignedIn: true)) { $0.voicePlace = .thisMac }
            },
            Entry("wizard-models-this-mac-both", size: size) {
                wizard(.models, model: fresh()) {
                    $0.voicePlace = .thisMac
                    $0.workPlace = .thisMac
                    $0.workModel = "gemma-4-12b"
                }
            },
            Entry("wizard-models-voice-model", size: size) {
                wizard(.models, model: fresh(models: separateVoiceModel, voiceLLMRows: true)) {
                    $0.voicePlace = .thisMac
                    $0.workPlace = .thisMac
                }
            },
            Entry("wizard-models-elsewhere", size: size) {
                wizard(.models, model: fresh()) {
                    $0.voicePlace = .elsewhere
                    $0.workPlace = .elsewhere
                }
            },
            Entry("wizard-models-downloading", size: size) {
                wizard(.models, model: {
                    let model = fresh(values: ["OPENAI_API_KEY": .string("(set)")],
                                      pull: .holding(done: 2_840_000_000, total: 7_100_000_000))
                    model.download("gemma-4-12b")
                    return model
                }()) {
                    $0.workPlace = .thisMac
                    $0.workModel = "gemma-4-12b"
                }
            },
            // The phone line
            Entry("wizard-phone", size: size) { wizard(.phone, model: fresh()) },
            Entry("wizard-phone-partway", size: size) {
                wizard(.phone, model: fresh(values: twilioAccount, phone: .connected))
            },
            Entry("wizard-phone-point-here", size: size) {
                wizard(.phone, model: fresh(values: twilioAccount.merging([
                    "TWILIO_NUMBER": .string("+15550001111"),
                ]) { $1 }, phone: .reachable))
            },
            Entry("wizard-phone-refused", size: size) {
                wizard(.phone, model: fresh(values: twilioAccount, phone: .refused))
            },
            Entry("wizard-phone-installing-tunnel", size: size) {
                wizard(.phone, model: {
                    let model = fresh(values: twilioAccount.merging(["TWILIO_NUMBER": .string("+15550001111")]) { $1 },
                                      phone: .connected)
                    model.installTunnel()
                    return model
                }())
            },
            Entry("wizard-phone-no-homebrew", size: size) {
                wizard(.phone, model: fresh(values: twilioAccount, phone: .connected, homebrew: nil))
            },
            // Checking, and how it ends
            Entry("wizard-checking", size: size) {
                wizard(.finish, model: checking(voiceDownload: .holding([
                    .step(step: "download", message: "Downloading the speech models"),
                    .progress(step: "download", done: 1_240_000_000, total: 2_900_000_000),
                ]))) { $0.voicePlace = .thisMac }
            },
            Entry("wizard-checking-failed", size: size) {
                wizard(.finish, model: checking(voiceDownload: .events([
                    .step(step: "download", message: "Downloading the speech models"),
                    .error(StreamFailure("The speech models couldn't be downloaded.",
                                         detail: "huggingface_hub: 503 Service Unavailable")),
                ]))) { $0.voicePlace = .thisMac }
            },
            Entry("wizard-checking-stopped", size: size) {
                wizard(.finish, model: checking(voiceDownload: .replay)) {
                    $0.voicePlace = .thisMac
                    $0.stopped = [.downloadSpeech]
                }
            },
            Entry("wizard-checking-review", size: size) {
                wizard(.finish, mode: .review, model: checking(voiceDownload: .replay)) { $0.voicePlace = .thisMac }
            },
            Entry("wizard-checking-details-left", size: size) {
                wizard(.finish, model: fresh(plan: ownerLeft, values: readyValues, claudeSignedIn: true,
                                             pin: PreviewData.pinFile, phone: .pointed))
            },
            Entry("wizard-ready", size: size) { wizard(.finish, model: ready()) },
            Entry("wizard-not-pointed", size: size) {
                wizard(.finish, model: fresh(plan: PreviewData.setupPlanReady, values: readyValues,
                                             claudeSignedIn: true, pin: PreviewData.pinFile, phone: .reachable))
            },
            Entry("wizard-ready-no-pin", size: size) { wizard(.finish, model: ready(pin: PreviewData.pinNone)) },
            Entry("wizard-nearly-ready", size: size) {
                wizard(.finish, model: fresh(plan: nearlyReady, values: ["OPENAI_API_KEY": .string("(set)")])) {
                    $0.phoneLater = true
                }
            },
            // You're Ready's extras
            Entry("wizard-profile", size: sheet) {
                ProfileSheet(questions: PreviewData.profileQuestions,
                             answers: ProfileAnswers(uses: ["coding", "email"], facts: ["Works nights.", "Prefers short answers."]))
                    .environment(fresh())
            },
            Entry("wizard-profile-preview", size: sheet) {
                ProfileSheet(questions: PreviewData.profileQuestions, preview: profilePreview)
                    .environment(fresh())
            },
            Entry("wizard-projects", size: sheet) {
                ProjectContextSheet(question: PreviewData.projectContextQuestion).environment(fresh())
            },
            Entry("wizard-projects-reading", size: sheet) {
                ProjectContextSheet(question: PreviewData.projectContextQuestion).environment({
                    let model = fresh(projectDraft: .holding([
                        .step(step: "dispatch", message: "Asking Claude to read your projects. This can take a few minutes."),
                        .step(step: "working", message: "Claude is reading your projects.", detail: "[tool] Read README.md"),
                    ]))
                    model.draftProjectContext(.explore)
                    return model
                }())
            },
            Entry("wizard-projects-draft", size: sheet) {
                ProjectContextSheet(question: PreviewData.projectContextQuestion).environment({
                    let model = fresh(projectDraft: .events([.done(projectDraft)]))
                    model.draftProjectContext(.explore)
                    return model
                }())
            },
        ]
    }

    // MARK: - Drawing a frame

    /// The wizard at `frame`, as if walked there from Welcome; `adjust` sets this run's
    /// choices (where the voice and the work run, the phone left for later).
    @MainActor private static func wizard(_ frame: WizardFrame, mode: SetupWizardMode = .firstRun, model: AppModel,
                                          adjust: (SetupWizard) -> Void = { _ in }) -> some View {
        let run = SetupWizard(mode: mode, path: path(to: frame), opening: model.wizardOpening.id)
        adjust(run)
        return SetupWizardView(wizard: run).environment(model)
    }

    /// One frame view itself, for a state its own fields hold (typed text).
    @MainActor private static func frame<V: View>(_ frame: WizardFrame, model: AppModel, _ view: () -> V) -> some View {
        let run = SetupWizard(path: path(to: frame))
        return view().environment(run).environment(model)
            .frame(width: WizardMetrics.size.width, height: WizardMetrics.size.height)
    }

    /// The frames up to `frame`, in order: what Back walks.
    private static func path(to frame: WizardFrame) -> [WizardFrame] {
        Array(WizardFrame.allCases.prefix { $0 != frame }.filter { $0 != .migrate || frame == .migrate }) + [frame]
    }

    // MARK: - Models

    enum PhoneState { case none, connected, reachable, pointed, refused }

    /// A new Mac (or `plan`), with `values` saved, Claude signed in or not, the PIN, the local
    /// models, the phone as far as `phone`, and scripts for the runs.
    @MainActor private static func fresh(
        plan: SetupPlan = PreviewData.setupPlan, values: [String: JSONValue] = [:], claudeSignedIn: Bool = false,
        pin: PinStatus = PreviewData.pinNone, models: ModelsReport = PreviewData.models, voiceLLMRows: Bool = false,
        phone: PhoneState = .none, homebrew: String? = "/opt/homebrew/bin/brew", pull: PreviewClient.Pull = .replay,
        voiceDownload: PreviewClient.Script<VoiceModelsDownloaded> = .replay,
        projectDraft: PreviewClient.Script<ProjectDraft> = .replay
    ) -> AppModel {
        var config = PreviewData.config
        // A new Mac: no OpenAI key until `values` says so.
        set(&config, "OPENAI_API_KEY", nil)
        set(&config, "PUBLIC_HOST", nil)
        for (key, value) in values { set(&config, key, value) }
        if voiceLLMRows { config += voiceLLMSettings }
        let numbers: PhoneNumbers
        let tunnel: PhoneTunnel
        let webhooks: PhoneWebhooks
        switch phone {
        case .none:
            (numbers, tunnel, webhooks) = (PreviewData.phoneNumbersUnset, PreviewData.phoneTunnelUnset,
                                           PreviewData.phoneWebhooksUnset)
        case .connected:
            (numbers, tunnel, webhooks) = (PreviewData.phoneNumbers, PreviewData.phoneTunnelUnset,
                                           PreviewData.phoneWebhooksUnset)
        case .reachable:
            set(&config, "PUBLIC_HOST", .string("keryx.example.com"))
            (numbers, tunnel, webhooks) = (PreviewData.phoneNumbers, PreviewData.phoneTunnel, PreviewData.phoneWebhooks)
        case .pointed:
            set(&config, "PUBLIC_HOST", .string("keryx.example.com"))
            (numbers, tunnel, webhooks) = (PreviewData.phoneNumbers, PreviewData.phoneTunnel,
                                           PreviewData.phoneWebhooksMatching)
        case .refused:
            var refused = PreviewData.phoneNumbersUnset
            refused.error = "Twilio refused the account SID and auth token (401)."
            (numbers, tunnel, webhooks) = (refused, PreviewData.phoneTunnelUnset, PreviewData.phoneWebhooksUnset)
        }
        var voice = PreviewData.voice
        voice.openaiKeySet = values["OPENAI_API_KEY"] != nil
        return AppModel.preview(client: PreviewClient(
            doctor: .success(claudeSignedIn ? signedIn : PreviewData.doctor), models: .success(models),
            config: .success(config), voice: .success(voice), pull: pull, setupPlan: .success(plan), pin: .success(pin),
            phoneNumbers: .success(numbers), phoneWebhooks: .success(webhooks), phoneTunnel: .success(tunnel),
            voiceDownload: voiceDownload, projectDraft: projectDraft, homebrew: homebrew))
    }

    /// Part way: the owner and the PIN done, the voice and the work still to set.
    @MainActor private static func partway() -> AppModel {
        fresh(plan: PreviewData.setupPlanPartway,
              values: ["OWNER_NAME": .string("Ada"), "ALLOWED_CALLERS": .array([.string("+15550100")])],
              pin: PreviewData.pinFile)
    }

    /// What a Mac ready for its first call has saved.
    private static let readyValues: [String: JSONValue] = twilioAccount.merging([
        "OPENAI_API_KEY": .string("(set)"), "OWNER_NAME": .string("Ada"),
        "ALLOWED_CALLERS": .array([.string("+15550100100"), .string("+447700900123")]),
        "OWNER_NUMBER": .string("+15550100100"), "TWILIO_NUMBER": .string("+15550001111"),
    ]) { $1 }

    /// Everything set: ready for a first call on +1 555 000 1111, which rings this Mac.
    @MainActor private static func ready(pin: PinStatus = PreviewData.pinFile) -> AppModel {
        fresh(plan: PreviewData.setupPlanReady, values: readyValues, claudeSignedIn: true, pin: pin, phone: .pointed)
    }

    /// Ready but for the owner's details, as today's CLI holds a blank name.
    private static var ownerLeft: SetupPlan {
        var plan = PreviewData.setupPlanReady
        plan.ready = false
        if let index = plan.steps.firstIndex(where: { $0.id == "owner" }) {
            plan.steps[index].state = SetupStepState.needed.rawValue
            plan.steps[index].why = "Owner name: OWNER_NAME is not set."
        }
        return plan
    }

    /// Checking on a Mac whose voice runs here: the speech models still to download, the
    /// work signed in, the phone done and the service to install.
    @MainActor private static func checking(voiceDownload: PreviewClient.Script<VoiceModelsDownloaded>) -> AppModel {
        var plan = done(PreviewData.setupPlanReady, "voice")
        plan.ready = false
        if let index = plan.steps.firstIndex(where: { $0.id == "voice" }) {
            plan.steps[index].state = SetupStepState.needed.rawValue
            plan.steps[index].why = "Voice server: the speech models are not downloaded yet."
        }
        if let index = plan.steps.firstIndex(where: { $0.id == "service" }) {
            plan.steps[index].state = SetupStepState.blocked.rawValue
            plan.steps[index].blockedBy = ["voice"]
        }
        return fresh(plan: plan, values: twilioAccount.merging(["TWILIO_NUMBER": .string("+15550001111")]) { $1 },
                     claudeSignedIn: true, pin: PreviewData.pinFile, phone: .reachable, voiceDownload: voiceDownload)
    }

    /// A Mac whose old Jarvis files must move first: `migrate` leads, and the rest waits.
    @MainActor private static func migrating(
        plan: Result<MigrationPlan, KeryxError> = .success(PreviewData.migrationPlan),
        run: PreviewClient.Script<MigrationDone> = .replay
    ) -> AppModel {
        AppModel.preview(client: PreviewClient(setupPlan: .success(migratingPlan), pin: .success(PreviewData.pinNone),
                                               migrationPlan: plan, migration: run))
    }

    private static func set(_ config: inout [ConfigRow], _ key: String, _ value: JSONValue?) {
        guard let index = config.firstIndex(where: { $0.key == key }) else { return }
        config[index].set = value != nil
        config[index].value = config[index].secret ? nil : value
    }

    private static let twilioAccount: [String: JSONValue] = [
        "TWILIO_ACCOUNT_SID": .string("AC0123456789abcdef0123456789abcdef"), "TWILIO_AUTH_TOKEN": .string("(set)"),
    ]

    /// `plan` with `id` done.
    private static func done(_ plan: SetupPlan, _ id: String) -> SetupPlan {
        var plan = plan
        if let index = plan.steps.firstIndex(where: { $0.id == id }) {
            plan.steps[index].state = SetupStepState.done.rawValue
            plan.steps[index].why = nil
            plan.steps[index].fix = nil
        }
        return plan
    }

    /// Everything but the phone, and the service that waits for it.
    private static var nearlyReady: SetupPlan {
        var plan = PreviewData.setupPlanReady
        plan.ready = false
        for (id, state, blocked) in [("phone", SetupStepState.needed, [String]()), ("service", .blocked, ["phone"])] {
            if let index = plan.steps.firstIndex(where: { $0.id == id }) {
                plan.steps[index].state = state.rawValue
                plan.steps[index].blockedBy = blocked
            }
        }
        return plan
    }

    /// The doctor with Claude signed in on its plan.
    private static var signedIn: DoctorReport {
        var doctor = PreviewData.doctor
        if let index = doctor.checks.firstIndex(where: { $0.name.hasPrefix("Claude Code agent") }) {
            doctor.checks[index].ok = true
            doctor.checks[index].state = "ok"
            doctor.checks[index].fix = nil
            doctor.checks[index].detail = "stored subscription login"
        }
        return doctor
    }

    /// SEAM: the models list as the CLI will print it with the owner's rule applied — a
    /// voice model of its own fits beside the agent's — written by hand.
    private static var separateVoiceModel: ModelsReport {
        var report = PreviewData.models
        report.recommended = "qwen3-coder-30b-a3b"
        report.recommendedWithLocalVoice = "qwen3-coder-30b-a3b"
        report.voiceLlmRecommended = "qwen3.5-4b"
        report.voiceLlmSeparate = true
        return report
    }

    /// SEAM: the voice's own model's settings, as `config list` will list them.
    private static var voiceLLMSettings: [ConfigRow] {
        ["VOICE_LLM": "shared", "VOICE_LLM_MODEL": ""].sorted { $0.key < $1.key }.map { key, value in
            ConfigRow(key: key, group: "local", title: key == "VOICE_LLM" ? "Voice's words" : "Voice's model",
                      description: "", source: "default", type: "string", required: false, set: false, secret: false,
                      serviceWritable: false, protected: false, value: value.isEmpty ? nil : .string(value),
                      choices: nil)
        }
    }

    /// A Mac whose old Jarvis files must move first (`keryx setup plan` while the storage
    /// refusal stands), written by hand from the new-Mac plan.
    private static var migratingPlan: SetupPlan {
        var plan = PreviewData.setupPlan
        let json = """
        {"id": "migrate", "title": "Migration", "required": true, "state": "needed",
         "why": "Storage: /Users/you/.jarvis still holds Keryx's files from when it was Jarvis.",
         "blocked_by": [], "walked": false, "checks": [], "fix": "migrate"}
        """
        guard let migrate = try? JSON.decoder.decode(SetupStep.self, from: Data(json.utf8)) else {
            fatalError("Setup+Catalog: the migrate step does not decode")
        }
        plan.steps = plan.steps.map { step in
            var waiting = step
            waiting.blockedBy = ["migrate"] + step.blockedBy
            waiting.state = SetupStepState.blocked.rawValue
            return waiting
        }
        plan.steps.insert(migrate, at: 0)
        return plan
    }

    /// What "About you" would write, as `--preview` says it.
    private static let profilePreview = ProfileResult(
        status: "written", written: false,
        text: """
        # What Lyra knows about the owner

        ## Standing facts

        - Mostly uses Lyra for coding tasks in their repositories and email triage.
        - Works nights.
        - Prefers short answers.

        ## Ongoing threads

        ## Recent calls
        """,
        chars: 214, maxChars: 4000, problems: [], walked: false)

    /// A draft of three projects and two facts, as the agent would write it.
    private static let projectDraft = ProjectDraft(
        summary: "Claude drafted 3 project summaries and 2 things about you for you to check.",
        projects: [
            DraftProject(name: "keryx", path: "/Users/you/code/keryx",
                         summary: "A phone voice assistant: Twilio calls, the OpenAI Realtime voice, and coding agents.",
                         known: true, addsProject: false),
            DraftProject(name: "recipes", path: "/Users/you/code/recipes",
                         summary: "A small web app for keeping recipes; TypeScript and SQLite.",
                         known: false, addsProject: true),
            DraftProject(name: "dotfiles", path: "/Users/you/dotfiles", summary: "Shell and editor configuration.",
                         known: false, addsProject: true),
        ],
        facts: ["Works nights.", "Writes Swift and Python."], dropped: [],
        folders: ["/Users/you"])
}
