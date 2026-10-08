import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
import KeryxTestSupport
@testable import KeryxUI

/// The setup wizard's rules: which frames a run shows and in what order (first run and
/// review), where it opens and when by itself, what each frame writes, how This Mac's work
/// is sequenced, and how Checking ends.
@MainActor @Suite struct SetupWizardTests {
    let fresh = PreviewData.setupPlan
    let partway = PreviewData.setupPlanPartway
    let ready = PreviewData.setupPlanReady

    /// `plan` with each of `ids` in `state`.
    func with(_ plan: SetupPlan, _ ids: [String], _ state: SetupStepState, deferred: Bool? = nil,
              deferrable: Bool? = nil) -> SetupPlan {
        var plan = plan
        for id in ids {
            guard let index = plan.steps.firstIndex(where: { $0.id == id }) else { continue }
            plan.steps[index].state = state.rawValue
            if let deferred { plan.steps[index].deferred = deferred }
            if let deferrable { plan.steps[index].deferrable = deferrable }
        }
        return plan
    }

    /// The plan with a `migrate` step leading.
    var migrating: SetupPlan {
        var plan = fresh
        let migrate = SetupStep(id: "migrate", title: "Migration", required: true, state: "needed", why: nil,
                                blockedBy: [], walked: false, checks: [], fix: "migrate")
        plan.steps.insert(migrate, at: 0)
        return plan
    }

    /// Every frame a run shows from Welcome, Continue after Continue.
    func walk(_ mode: SetupWizardMode, _ plan: SetupPlan, config: [ConfigRow]? = nil) -> [WizardFrame] {
        let wizard = SetupWizard(mode: mode)
        let facts = WizardFacts(plan: plan, config: config)
        while wizard.advance(facts) {}
        return wizard.path
    }

    // MARK: - Sequencing

    @Test func aNewMacWalksSixFrames() {
        #expect(walk(.firstRun, fresh) == [.welcome, .aboutYou, .models, .pin, .phone, .finish])
    }

    @Test func theMigrationComesFirstOnlyWhileFilesAreStillToMove() {
        #expect(walk(.firstRun, migrating) == [.welcome, .migrate, .aboutYou, .models, .pin, .phone, .finish])
        #expect(walk(.review, migrating).contains(.migrate))
        #expect(!walk(.review, fresh).contains(.migrate), "review shows it only when it is needed")
        #expect(!walk(.firstRun, with(migrating, ["migrate"], .done)).contains(.migrate))
    }

    @Test func firstRunSkipsAFrameWithNothingLeft() {
        // The owner and the PIN are done part way.
        #expect(walk(.firstRun, partway) == [.welcome, .models, .phone, .finish])
        // Models needs both its steps done to be skipped.
        let voiceDone = with(partway, ["voice"], .done)
        #expect(walk(.firstRun, voiceDone).contains(.models))
        #expect(!walk(.firstRun, with(voiceDone, ["agents"], .done)).contains(.models))
    }

    @Test func onlyTheServiceLeftIsWelcomeThenChecking() {
        let serviceLeft = with(ready, ["service"], .needed)
        #expect(walk(.firstRun, serviceLeft) == [.welcome, .finish])
        #expect(walk(.firstRun, ready) == [.welcome, .finish])
    }

    @Test func reviewShowsEveryFramePrefilled() {
        #expect(walk(.review, ready) == [.welcome, .aboutYou, .models, .pin, .phone, .finish])
        #expect(walk(.review, partway) == [.welcome, .aboutYou, .models, .pin, .phone, .finish])
    }

    @Test func theDotStripCountsOnlyTheFramesThisRunShows() {
        let wizard = SetupWizard()
        let facts = WizardFacts(plan: partway)
        let start = wizard.position(facts)
        #expect(start.index == 1 && start.count == 4)
        wizard.advance(facts)
        #expect(wizard.current == .models)
        let at = wizard.position(facts)
        #expect(at.index == 2 && at.count == 4)
        // What is done since stays counted where it was walked.
        wizard.advance(WizardFacts(plan: with(partway, ["voice", "agents"], .done)))
        let after = wizard.position(WizardFacts(plan: with(partway, ["voice", "agents"], .done)))
        #expect(after.index == 3 && after.count == 4)
    }

    @Test func backReturnsToTheFrameShownAndFixGoesStraightThere() {
        let wizard = SetupWizard()
        let facts = WizardFacts(plan: fresh)
        #expect(!wizard.back(), "nothing before Welcome")
        wizard.advance(facts)
        wizard.advance(facts)
        #expect(wizard.current == .models)
        #expect(wizard.back())
        #expect(wizard.current == .aboutYou)
        while wizard.advance(facts) {}
        #expect(wizard.current == .finish)
        wizard.go(to: .phone)
        #expect(wizard.current == .phone)
        wizard.back()
        #expect(wizard.current == .finish, "Fix… keeps the way back")
    }

    @Test func aNewOpeningStartsAgainAtWelcomeInItsMode() {
        let wizard = SetupWizard()
        let facts = WizardFacts(plan: fresh)
        wizard.advance(facts)
        wizard.phoneLater = true
        wizard.voicePlace = .thisMac
        let review = WizardOpening(mode: .review)
        wizard.start(review)
        #expect(wizard.path == [.welcome] && wizard.mode == .review)
        #expect(!wizard.phoneLater && wizard.voicePlace == nil)
        wizard.advance(facts)
        wizard.start(review)
        #expect(wizard.current == .aboutYou, "the same opening again changes nothing")
    }

    @Test func resumingIsFirstRunWithSomethingRequiredAlreadySettled() {
        #expect(!WizardFlow.resuming(mode: .firstRun, facts: WizardFacts(plan: fresh)))
        #expect(WizardFlow.resuming(mode: .firstRun, facts: WizardFacts(plan: partway)))
        #expect(!WizardFlow.resuming(mode: .review, facts: WizardFacts(plan: partway)))
        #expect(!WizardFlow.resuming(mode: .firstRun, facts: WizardFacts(plan: ready)))
    }

    // MARK: - When it opens

    @Test func itOpensAtLaunchOnceWhileNotReadyAndNotTurnedOff() {
        #expect(SetupLaunch.shouldOpen(plan: fresh, hidden: false, alreadyOpened: false))
        #expect(!SetupLaunch.shouldOpen(plan: ready, hidden: false, alreadyOpened: false))
        #expect(!SetupLaunch.shouldOpen(plan: fresh, hidden: true, alreadyOpened: false))
        #expect(!SetupLaunch.shouldOpen(plan: fresh, hidden: false, alreadyOpened: true))
        #expect(!SetupLaunch.shouldOpen(plan: nil, hidden: false, alreadyOpened: false), "no plan, no window")
    }

    @Test func whatWasLeftForLaterIsNoReasonToOpenAgain() {
        var plan = with(ready, ["phone"], .needed, deferred: true, deferrable: true)
        plan.ready = false
        #expect(plan.nearlyReady)
        #expect(!SetupLaunch.shouldOpen(plan: plan, hidden: false, alreadyOpened: false))
    }

    @Test func turningItOffIsRememberedInTheAppsDefaults() {
        let defaults = UserDefaults(suiteName: "SetupWizardTests-\(UUID().uuidString)")!
        #expect(!SetupLaunch.isHidden(in: defaults))
        defaults.set(true, forKey: SetupLaunch.hiddenKey)
        #expect(SetupLaunch.isHidden(in: defaults))
    }

    @Test func theMenuOpensItInReviewAndTheChecklistInFirstRun() {
        let model = AppModel.preview()
        let before = model.wizardOpening.id
        var opened = false
        let actions = MenuActions(openWindow: {}, openSession: { _ in }, quit: {}, openSetup: {
            model.openSetupWizard(.review)
            opened = true
        })
        actions.openSetup()
        #expect(opened && model.wizardOpening.mode == .review && model.wizardOpening.id != before)
        model.openSetupWizard(.firstRun)
        #expect(model.wizardOpening.mode == .firstRun)
        #expect(KeryxWindowID.setup == "setup")
    }

    // MARK: - About You

    @Test func aPhoneNumberIsE164AsTyped() {
        #expect(CallerNumbers.normalised("+44 7700 900123") == "+447700900123")
        #expect(CallerNumbers.normalised("+1 (555) 010-0199") == "+15550100199")
        #expect(CallerNumbers.normalised("07700 900123") == nil)
        #expect(CallerNumbers.normalised("+0123456789") == nil)
        #expect(CallerNumbers.normalised("+1555") == nil)
        #expect(CallerNumbers.problem("07700") == "Start with + and the country code, as in +44 7700 900123.")
        #expect(CallerNumbers.problem("+44") == "That isn't a whole phone number yet.")
        #expect(CallerNumbers.problem("") == nil)
        #expect(CallerNumbers.list(["+15550100", " ", "+15550100"]) == ["+15550100"])
        #expect(CallerNumbers.list(["+15550100", "0770"]) == nil, "one field wrong holds Continue")
        #expect(CallerNumbers.list([""]) == nil, "at least one")
    }

    @Test func aCodingAgentsNameIsRefusedForTheAssistant() {
        #expect(AboutYouAnswers.assistantProblem("Claude") == "That's a coding agent's name.")
        #expect(AboutYouAnswers.assistantProblem("codex") != nil)
        #expect(AboutYouAnswers.assistantProblem("Vega") == nil)
    }

    @Test func aboutYouWritesOnlyWhatChanged() {
        let config = PreviewData.config
        let facts = WizardFacts(plan: fresh, config: config)
        let answers = AboutYouAnswers(name: "Ada", assistant: "Lyra", numbers: ["+1 555 0100", "+44 7700 900123"])
        #expect(answers.writes(from: facts) == [
            .set("OWNER_NAME", "Ada"),
            .set("ALLOWED_CALLERS", "+15550100,+447700900123"),
            .set("OWNER_NUMBER", "+15550100"),
        ], "the assistant's name is the default already")
        // Saved as typed: nothing to write.
        let saved = WizardFacts(plan: fresh, config: settings(config, [
            "OWNER_NAME": .string("Ada"), "OWNER_NUMBER": .string("+15550100"),
            "ALLOWED_CALLERS": .array([.string("+15550100"), .string("+447700900123")]),
        ]))
        #expect(answers.writes(from: saved).isEmpty)
        #expect(CallerNumbers.saved(saved) == ["+15550100", "+447700900123"])
        // A blank name unsets one saved; a new name for the assistant is set.
        let renamed = AboutYouAnswers(name: " ", assistant: "Vega", numbers: ["+15550100", "+447700900123"])
        #expect(renamed.writes(from: saved) == [.unset("OWNER_NAME"), .set("ASSISTANT_NAME", "Vega")])
        // The number rung first moves to the front.
        let swapped = AboutYouAnswers(name: "Ada", assistant: "", numbers: ["+447700900123", "+15550100"])
        #expect(swapped.writes(from: saved) == [
            .set("ALLOWED_CALLERS", "+447700900123,+15550100"), .set("OWNER_NUMBER", "+447700900123"),
        ])
    }

    @Test func aboutYouWritesThroughConfigSet() async throws {
        let (model, fake) = Self.model()
        let error = await WizardWrite.apply(
            AboutYouAnswers(name: "Ada", assistant: "", numbers: ["+15550100"])
                .writes(from: WizardFacts(plan: fresh, config: PreviewData.config)),
            with: model)
        #expect(error == nil)
        #expect(Self.written(fake) == [
            ["config", "set", "OWNER_NAME", "Ada"], ["config", "set", "ALLOWED_CALLERS", "+15550100"],
            ["config", "set", "OWNER_NUMBER", "+15550100"],
        ])
    }

    // MARK: - Models

    @Test func theDefaultsAreOpenAIAndClaudeUnlessSomethingElseIsSetUp() {
        let facts = WizardFacts(plan: fresh, config: PreviewData.config)
        #expect(VoicePlace.current(PreviewData.voice) == .openai)
        #expect(VoicePlace.current(PreviewData.voiceHere) == .thisMac)
        #expect(VoicePlace.current(nil) == .openai)
        #expect(WorkPlace.current(facts, models: PreviewData.models) == .claude)
        let local = WizardFacts(plan: fresh, config: settings(PreviewData.config, ["AGENT_BACKEND": .string("local")]))
        #expect(WorkPlace.current(local, models: PreviewData.models) == .thisMac)
    }

    @Test func eachSectionIsAnsweredNotFinished() {
        let facts = WizardFacts(plan: fresh, config: PreviewData.config)
        #expect(ModelsRules.voiceAnswered(.openai, voice: nil, keySaved: true, wordsModel: nil))
        #expect(!ModelsRules.voiceAnswered(.openai, voice: nil, keySaved: false, wordsModel: nil))
        #expect(ModelsRules.voiceAnswered(.thisMac, voice: nil, keySaved: false, wordsModel: "gemma-4-12b"),
                "a model not yet downloaded is an answer")
        #expect(!ModelsRules.voiceAnswered(.elsewhere, voice: PreviewData.voice, keySaved: true, wordsModel: nil))
        #expect(ModelsRules.voiceAnswered(.elsewhere, voice: PreviewData.voiceServer, keySaved: false, wordsModel: nil))
        #expect(ModelsRules.workAnswered(.claude, signedIn: false, keySaved: true, model: nil, facts: facts))
        #expect(!ModelsRules.workAnswered(.codex, signedIn: false, keySaved: false, model: nil, facts: facts))
        #expect(ModelsRules.workAnswered(.thisMac, signedIn: false, keySaved: false, model: "gemma-4-12b", facts: facts))
    }

    @Test func theWorkModelIsTheOneChosenElseTheRecommendation() {
        #expect(ModelsRules.workModel(PreviewData.models, voiceHere: false) == "qwen3-coder-30b-a3b", "chosen")
        var none = PreviewData.models
        none.servers.llm.model = nil
        #expect(ModelsRules.workModel(none, voiceHere: false) == "qwen3-coder-30b-a3b", "the catalog's recommended")
        none.recommended = "gpt-oss-20b"
        none.recommendedWithLocalVoice = "gemma-4-12b"
        #expect(ModelsRules.workModel(none, voiceHere: false) == "gpt-oss-20b", "SEAM: the CLI's word wins")
        #expect(ModelsRules.workModel(none, voiceHere: true) == "gemma-4-12b")
        #expect(ModelsRules.tight(PreviewData.models) == false)
    }

    @Test func theVoicesOwnModelIsAControlOnlyOnceTheCLIOffersIt() {
        let facts = WizardFacts(plan: fresh, config: PreviewData.config)
        #expect(!ModelsRules.voiceWordsIsAControl(PreviewData.models, facts: facts), "today: shared")
        var report = PreviewData.models
        report.voiceLlmSeparate = true
        report.voiceLlmRecommended = "qwen3.5-4b"
        #expect(!ModelsRules.voiceWordsIsAControl(report, facts: facts), "no setting to write it to yet")
        let withRow = WizardFacts(plan: fresh, config: PreviewData.config + [Self.row("VOICE_LLM_MODEL"),
                                                                           Self.row("VOICE_LLM")])
        #expect(ModelsRules.voiceWordsIsAControl(report, facts: withRow))
        #expect(ModelsRules.voiceWordsModel(report) == "qwen3.5-4b")
        report.voiceLlmSeparate = false
        #expect(ModelsRules.voiceWordsModel(report) == nil, "shared: the work's")
    }

    @Test func modelsWritesTheVoiceAndTheDefaultAgent() {
        let facts = WizardFacts(plan: fresh, config: PreviewData.config)
        // OpenAI in use already, Claude the default already: nothing.
        #expect(ModelsRules.writes(ModelsChoice(voice: .openai, work: .claude), facts: facts,
                                   voice: PreviewData.voice, models: PreviewData.models).isEmpty)
        // From a voice server back to OpenAI, and Codex as the one agent.
        #expect(ModelsRules.writes(ModelsChoice(voice: .openai, work: .codex), facts: facts,
                                   voice: PreviewData.voiceServer, models: PreviewData.models)
            == [.voiceOpenAI, .set("AGENT_BACKEND", "codex"), .set("AGENTS_ENABLED", "")])
        // This Mac's voice, a new voice picked; the work's model is chosen later, once downloaded.
        #expect(ModelsRules.writes(ModelsChoice(voice: .thisMac, work: .thisMac, speaksIn: "bf_emma"), facts: facts,
                                   voice: PreviewData.voice, models: PreviewData.models)
            == [.choose("VOICE_SERVER_VOICE", "bf_emma")])
    }

    @Test func aModelServerElsewhereBecomesTheDefaultAndThisMacsServerIsCleared() {
        let facts = WizardFacts(plan: fresh, config: PreviewData.config)
        let writes = ModelsRules.writes(ModelsChoice(voice: .openai, work: .elsewhere, serverModel: "gpt-oss-20b"),
                                        facts: facts, voice: PreviewData.voice, models: PreviewData.models)
        #expect(writes == [.set("LOCAL_AGENT_MODEL", "gpt-oss-20b"), .set("AGENT_BACKEND", "local"),
                           .set("AGENTS_ENABLED", ""), .unset("LLM_SERVER_MODEL")])
        // Once the CLI calls it AGENT_SERVER_MODEL, that is the key cleared.
        var renamed = PreviewData.config
        if let index = renamed.firstIndex(where: { $0.key == "LLM_SERVER_MODEL" }) { renamed[index].key = "AGENT_SERVER_MODEL" }
        #expect(ModelsRules.writes(ModelsChoice(voice: .openai, work: .elsewhere),
                                   facts: WizardFacts(plan: fresh, config: renamed), voice: PreviewData.voice,
                                   models: PreviewData.models).last == .unset("AGENT_SERVER_MODEL"))
    }

    @Test func theVoicesOwnModelIsWrittenWhenTheCLIHasTheSetting() {
        var report = PreviewData.models
        report.voiceLlmSeparate = true
        let facts = WizardFacts(plan: fresh, config: PreviewData.config + [Self.row("VOICE_LLM_MODEL"), Self.row("VOICE_LLM")])
        #expect(ModelsRules.writes(ModelsChoice(voice: .thisMac, work: .thisMac, wordsModel: "qwen3.5-4b"),
                                   facts: facts, voice: PreviewData.voice, models: report)
            == [.set("VOICE_LLM", "here"), .set("VOICE_LLM_MODEL", "qwen3.5-4b")])
        #expect(ModelsRules.writes(ModelsChoice(voice: .thisMac, work: .thisMac, wordsModel: ""),
                                   facts: facts, voice: PreviewData.voice, models: report)
            == [.set("VOICE_LLM", "shared")])
    }

    @Test func modelsWritesThroughTheVoiceAndConfigCommands() async throws {
        let (model, fake) = Self.model()
        _ = await WizardWrite.apply([.voiceOpenAI, .set("AGENT_BACKEND", "codex"), .set("AGENTS_ENABLED", ""),
                                     .unset("LLM_SERVER_MODEL")], with: model)
        #expect(Self.written(fake) == [
            ["voice", "use", "openai", "--json"], ["config", "set", "AGENT_BACKEND", "codex"],
            ["config", "set", "AGENTS_ENABLED", ""], ["config", "unset", "LLM_SERVER_MODEL"],
        ])
    }

    // MARK: - This Mac's work

    @Test func thisMacsWorkGoesDownloadUseSpeechThenVoice() {
        let facts = WizardFacts(plan: fresh, config: PreviewData.config)
        var report = PreviewData.models
        let both = LocalIntent(voiceHere: true, workHere: true, model: "gemma-4-12b")
        #expect(LocalWork.next(both, models: report, voice: PreviewData.voice, facts: facts, busy: false)
            == .download("gemma-4-12b"))
        #expect(LocalWork.next(both, models: report, voice: PreviewData.voice, facts: facts, busy: true) == .wait)
        let index = report.models.firstIndex { $0.name == "gemma-4-12b" }!
        report.models[index].downloaded = true
        #expect(LocalWork.next(both, models: report, voice: PreviewData.voice, facts: facts, busy: false)
            == .use("gemma-4-12b", defaultAgent: true))
        report.servers.llm.model = "gemma-4-12b"
        let local = WizardFacts(plan: fresh, config: settings(PreviewData.config, ["AGENT_BACKEND": .string("local")]))
        #expect(LocalWork.next(both, models: report, voice: PreviewData.voice, facts: local, busy: false)
            == .downloadSpeech)
        var voice = PreviewData.voice
        voice.here.modelsReady = true
        #expect(LocalWork.next(both, models: report, voice: voice, facts: local, busy: false) == .setUpVoice)
        #expect(LocalWork.next(both, models: report, voice: PreviewData.voiceHere, facts: local, busy: false) == .done)
    }

    @Test func aWorkModelHereWithACloudVoiceStartsItsServer() {
        let local = WizardFacts(plan: fresh, config: settings(PreviewData.config, ["AGENT_BACKEND": .string("local")]))
        var report = PreviewData.models
        report.servers.llm.reachable = false
        let work = LocalIntent(voiceHere: false, workHere: true, model: "qwen3-coder-30b-a3b")
        #expect(LocalWork.next(work, models: report, voice: PreviewData.voice, facts: local, busy: false) == .startServer)
        #expect(LocalWork.next(work, models: PreviewData.models, voice: PreviewData.voice, facts: local, busy: false)
            == .done)
        // A voice here with the work in the cloud: its words model is chosen without the default.
        let voiceOnly = LocalIntent(voiceHere: true, workHere: false, model: "gpt-oss-20b")
        let facts = WizardFacts(plan: fresh, config: PreviewData.config)
        #expect(LocalWork.next(voiceOnly, models: PreviewData.models, voice: PreviewData.voice, facts: facts, busy: false)
            == .use("gpt-oss-20b", defaultAgent: false))
        #expect(LocalWork.next(LocalIntent(voiceHere: false, workHere: false, model: nil), models: report,
                               voice: nil, facts: facts, busy: true) == .done, "nothing here")
    }

    @Test func theRunsChoicesWinOverWhatIsSetUp() {
        let wizard = SetupWizard()
        let facts = WizardFacts(plan: fresh, config: PreviewData.config)
        let today = LocalIntent.of(wizard, facts: facts, voice: PreviewData.voice, models: PreviewData.models)
        #expect(today == LocalIntent(voiceHere: false, workHere: false, model: nil))
        wizard.voicePlace = .thisMac
        wizard.voiceWordsModel = "gemma-4-12b"
        #expect(LocalIntent.of(wizard, facts: facts, voice: PreviewData.voice, models: PreviewData.models)
            == LocalIntent(voiceHere: true, workHere: false, model: "gemma-4-12b"))
    }

    // MARK: - The PIN and the phone

    @Test func thePinLeftToTheFirstCallIsSetupMark() async throws {
        let (model, fake) = Self.model()
        _ = await WizardWrite.apply([.mark("pin")], with: model)
        #expect(Self.written(fake) == [["setup", "mark", "pin", "--json"]])
    }

    @Test func thePhoneSavesWhatIsShownAndTypedNeverPointingItHere() {
        let config = settings(PreviewData.config, ["TWILIO_ACCOUNT_SID": .string("AC0123"), "TWILIO_AUTH_TOKEN": .string("(set)")])
        let facts = WizardFacts(plan: fresh, config: config)
        let steps = PhoneSteps(facts: facts, numbers: PreviewData.phoneNumbers, tunnel: PreviewData.phoneTunnel,
                               webhooks: PreviewData.phoneWebhooks)
        #expect(steps.accountDone && !steps.numberDone)
        #expect(steps.canContinue(pickedNumber: nil, typedHost: "keryx.example.com"))
        #expect(steps.writes(pickedNumber: nil, typedHost: "calls.example.org")
            == [.set("TWILIO_NUMBER", "+15550001111"), .set("PUBLIC_HOST", "calls.example.org")])
        #expect(steps.writes(pickedNumber: nil, typedHost: "keryx.example.com") == [.set("TWILIO_NUMBER", "+15550001111")],
                "the hostname saved already")
        #expect(!steps.writes(pickedNumber: "+15550002222", typedHost: "").contains(.mark("phone")))
        let unset = PhoneSteps(facts: WizardFacts(plan: fresh, config: PreviewData.config), numbers: PreviewData.phoneNumbersUnset,
                               tunnel: PreviewData.phoneTunnelUnset, webhooks: nil)
        #expect(!unset.canContinue(pickedNumber: nil, typedHost: "keryx.example.com"), "no account, no Continue")
    }

    @Test func setUpLaterIsRecordedOnlyWhereTheCLICanDefer() {
        #expect(PhoneSteps.later(WizardFacts(plan: fresh)).isEmpty, "today: this run remembers it")
        let deferrable = with(fresh, ["phone"], .blocked, deferrable: true)
        #expect(PhoneSteps.later(WizardFacts(plan: deferrable)) == [.mark("phone")])
    }

    // MARK: - Checking

    @Test func checkingEndsReadyNearlyReadyOrStillChecking() {
        #expect(CheckingRules.ending(ready, phoneLater: false) == .ready)
        #expect(CheckingRules.ending(fresh, phoneLater: true) == .checking)
        #expect(CheckingRules.ending(nil, phoneLater: true) == .checking)
        // Today: the phone left in this run, and the service waiting only for it.
        var phoneLeft = with(ready, ["phone"], .needed)
        phoneLeft = with(phoneLeft, ["service"], .blocked)
        if let index = phoneLeft.steps.firstIndex(where: { $0.id == "service" }) { phoneLeft.steps[index].blockedBy = ["phone"] }
        phoneLeft.ready = false
        #expect(CheckingRules.ending(phoneLeft, phoneLater: true) == .nearlyReady)
        #expect(CheckingRules.ending(phoneLeft, phoneLater: false) == .checking)
        // SEAM: the plan says what was deferred, whatever this run remembers.
        var deferred = with(ready, ["phone"], .needed, deferred: true, deferrable: true)
        deferred.ready = false
        #expect(CheckingRules.ending(deferred, phoneLater: false) == .nearlyReady)
        let notDeferred = with(deferred, ["phone"], .needed, deferred: false)
        #expect(CheckingRules.ending(notDeferred, phoneLater: true) == .checking)
    }

    @Test func theServiceIsInstalledOnceItWaitsForNothing() {
        #expect(!CheckingRules.serviceInstallable(fresh), "blocked")
        #expect(CheckingRules.serviceInstallable(with(ready, ["service"], .needed)))
        #expect(CheckingRules.serviceInstallable(with(ready, ["service"], .failed)))
        #expect(!CheckingRules.serviceInstallable(ready))
    }

    @Test func aFailedRowFixesOnTheFrameThatSetsIt() {
        #expect(CheckRow.voice.frame == .models && CheckRow.work.frame == .models)
        #expect(CheckRow.phone.frame == .phone)
        #expect(LocalStep.downloadSpeech.row == .voice && LocalStep.startServer.row == .work)
    }

    // MARK: - Words

    @Test func welcomeSaysHowFarThereIsToGo() {
        #expect(WizardWording.welcome(mode: .firstRun, resuming: false, left: 6, frames: 6, assistant: "Lyra")
            .hasSuffix("Six short steps and you're set."))
        #expect(WizardWording.welcome(mode: .firstRun, resuming: true, left: 3, frames: 4, assistant: "Lyra")
            == "Pick up where you left off. Three things are still to set.")
        #expect(WizardWording.welcome(mode: .firstRun, resuming: true, left: 1, frames: 2, assistant: "Lyra")
            == "Pick up where you left off. One thing is still to set.")
        #expect(WizardWording.welcome(mode: .review, resuming: false, left: 0, frames: 6, assistant: "Lyra")
            == "Go through every setting again. What's set already is filled in.")
        #expect(WizardWording.welcomeButton(resuming: true) == "Continue Setup")
    }

    @Test func theMoveIsSaidInAFewLinesWithoutAPath() {
        let lines = WizardWording.moveSummary(PreviewData.migrationPlan)
        #expect(lines == ["9 files and folders move to Keryx's folders.",
                          "The background service stops while they move, and starts again after.",
                          "The Jarvis folder is renamed aside. Nothing is deleted."])
        #expect(lines.allSatisfy { !$0.contains("/") })
        #expect(WizardWording.moveSummary(PreviewData.migrationPlanNothing) == ["Nothing is left to move."])
        #expect(WizardPaths.short("/Users/you/.jarvis/pin") == "~/.jarvis/pin")
    }

    @Test func readyNamesTheNumberAndThePinStillToChoose() {
        #expect(WizardWording.ready("Lyra", number: "+15550100", pinSet: true)
            == "Lyra can take your first call. Ring +15550100 from your phone. Lyra will say hello and ask a little about you.")
        #expect(WizardWording.ready("Lyra", number: nil, pinSet: false).hasSuffix("and ask you to choose a PIN."))
    }

    @Test func everyFrameAndStateIsInTheCatalog() {
        let ids = Set(ScreenCatalog.entries().map(\.id))
        for id in ["wizard-welcome", "wizard-welcome-resuming", "wizard-welcome-review", "wizard-move-files",
                   "wizard-move-files-conflict", "wizard-move-files-moving", "wizard-move-files-failed",
                   "wizard-about-you", "wizard-about-you-review", "wizard-models", "wizard-models-this-mac-voice",
                   "wizard-models-this-mac-both", "wizard-models-voice-model", "wizard-models-elsewhere",
                   "wizard-pin", "wizard-phone", "wizard-checking", "wizard-checking-failed", "wizard-ready",
                   "wizard-nearly-ready", "wizard-profile", "wizard-projects-draft"] {
            #expect(ids.contains(id), "missing \(id)")
        }
    }

    // MARK: - Helpers

    /// A model over the real client, its commands answered by a fake runner.
    static func model() -> (AppModel, FakeRunner) {
        let fake = FakeRunner { request in
            let words = Array(request.arguments.dropFirst(KeryxLocation.fixture.prefixArguments.count))
            let body: String = switch words.first {
            case "voice": #"{"saved": {}, "restart_needed": true}"#
            case "setup": #"{"id": "pin", "walked": true}"#
            case "config" where words.dropFirst().first == "list": "[]"
            default: "{}"
            }
            return CommandResult(exitCode: 0, stdout: Data(body.utf8), stderr: Data())
        }
        let client = CLIClient(location: .fixture, runner: fake)
        let model = AppModel.preview(client: PreviewClient())
        model.client = client
        return (model, fake)
    }

    /// The writes the runner was asked for, as the CLI's words: every command but the reads
    /// that follow them.
    static func written(_ fake: FakeRunner) -> [[String]] {
        fake.requests.map { Array($0.arguments.dropFirst(KeryxLocation.fixture.prefixArguments.count)) }
            .filter { !["list", "plan", "options"].contains($0.dropFirst().first ?? "") && $0 != ["voice", "--json"] }
    }

    /// `config` with `values` saved.
    func settings(_ config: [ConfigRow], _ values: [String: JSONValue]) -> [ConfigRow] {
        var config = config
        for (key, value) in values {
            guard let index = config.firstIndex(where: { $0.key == key }) else { continue }
            config[index].set = true
            config[index].value = config[index].secret ? nil : value
        }
        return config
    }

    static func row(_ key: String) -> ConfigRow {
        ConfigRow(key: key, group: "local", description: "", source: "default", type: "string", required: false,
                  set: false, secret: false, serviceWritable: false, protected: false, value: nil, choices: nil)
    }
}
