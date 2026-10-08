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

    @Test func aNewMacWalksFiveFrames() {
        #expect(walk(.firstRun, fresh) == [.welcome, .aboutYou, .models, .phone, .finish])
        #expect(WizardFlow.questions(mode: .firstRun, facts: WizardFacts(plan: fresh)) == 3)
    }

    @Test func theMigrationComesFirstOnlyWhileFilesAreStillToMove() {
        #expect(walk(.firstRun, migrating) == [.welcome, .migrate, .aboutYou, .models, .phone, .finish])
        #expect(walk(.review, migrating).contains(.migrate))
        #expect(!walk(.review, fresh).contains(.migrate), "review shows it only when it is needed")
        #expect(!walk(.firstRun, with(migrating, ["migrate"], .done)).contains(.migrate))
    }

    @Test func firstRunSkipsAFrameWithNothingLeft() {
        // The owner and the PIN are done part way: About You has nothing left.
        #expect(walk(.firstRun, partway) == [.welcome, .models, .phone, .finish])
        // About You asks while either is left.
        #expect(walk(.firstRun, with(partway, ["pin"], .needed)).contains(.aboutYou))
        #expect(walk(.firstRun, with(partway, ["owner"], .needed)).contains(.aboutYou))
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
        #expect(walk(.review, ready) == [.welcome, .aboutYou, .models, .phone, .finish])
        #expect(walk(.review, partway) == [.welcome, .aboutYou, .models, .phone, .finish])
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

    @Test func aLaunchThatFindsSetupReadyRecordsIt() {
        // Set up from a terminal, or by an earlier app: ready at launch, so recorded…
        let defaults = UserDefaults(suiteName: "SetupWizardTests-\(UUID().uuidString)")!
        #expect(!SetupLaunch.atLaunch(plan: ready, hidden: false, alreadyOpened: false, defaults: defaults))
        #expect(SetupLaunch.completedOnce(in: defaults))
        // …and a sign-in that expires later opens nothing.
        var expired = with(ready, ["agents"], .needed)
        expired.ready = false
        #expect(!SetupLaunch.atLaunch(plan: expired, hidden: false, alreadyOpened: false, defaults: defaults))
        // A new Mac opens it, and records nothing.
        let fresher = UserDefaults(suiteName: "SetupWizardTests-\(UUID().uuidString)")!
        #expect(SetupLaunch.atLaunch(plan: fresh, hidden: false, alreadyOpened: false, defaults: fresher))
        #expect(!SetupLaunch.completedOnce(in: fresher))
        // Nearly ready (all that is left was deferred) counts as seen through.
        var deferred = with(ready, ["phone"], .needed, deferred: true, deferrable: true)
        deferred.ready = false
        let nearly = UserDefaults(suiteName: "SetupWizardTests-\(UUID().uuidString)")!
        _ = SetupLaunch.atLaunch(plan: deferred, hidden: false, alreadyOpened: false, defaults: nearly)
        #expect(SetupLaunch.completedOnce(in: nearly))
    }

    @Test func onlyReachingTheEndRecordsSetupAsSeenThrough() {
        #expect(CheckingRules.completes(.ready) && CheckingRules.completes(.nearlyReady))
        #expect(CheckingRules.completes(.notPointed))
        #expect(!CheckingRules.completes(.checking), "Close on Checking, or closing the window, records nothing")
        #expect(SetupWizard().recordsCompletion)
        let catalog = SetupWizard()
        _ = SetupWizardView(wizard: catalog)
        #expect(!catalog.recordsCompletion, "the catalog's runs record nothing")
    }

    @Test func onceSeenThroughItNeverOpensByItselfAgain() {
        // Was ready, then a sign-in expired: the plan is not ready, and still nothing opens.
        var notReady = with(ready, ["agents"], .needed)
        notReady.ready = false
        #expect(SetupLaunch.shouldOpen(plan: notReady, hidden: false, alreadyOpened: false))
        #expect(!SetupLaunch.shouldOpen(plan: notReady, hidden: false, alreadyOpened: false, completedOnce: true))
        let defaults = UserDefaults(suiteName: "SetupWizardTests-\(UUID().uuidString)")!
        #expect(!SetupLaunch.completedOnce(in: defaults))
        SetupLaunch.markCompleted(in: defaults)
        #expect(SetupLaunch.completedOnce(in: defaults))
    }

    @Test func eachEntryOpensItInItsMode() {
        #expect(SetupEntry.menu.mode == .review)
        #expect(SetupEntry.checklist.mode == .firstRun)
        #expect(SetupEntry.launch.mode == .firstRun)
        let model = AppModel.preview()
        let before = model.wizardOpening.id
        SetupLaunch.open(.menu, model: model)
        #expect(model.wizardOpening.mode == .review && model.wizardOpening.id != before)
        let review = model.wizardOpening.id
        SetupLaunch.open(.checklist, model: model)
        #expect(model.wizardOpening.mode == .firstRun && model.wizardOpening.id != review,
                "a second opening starts the window again at Welcome")
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

    @Test func theVoicesOwnModelIsAControlOnceTheCLIHasAVoiceLLM() {
        let facts = WizardFacts(plan: fresh, config: PreviewData.config)
        #expect(ModelsRules.voiceWordsIsAControl(facts: facts))
        let older = WizardFacts(plan: fresh, config: PreviewData.config.filter { $0.key != "VOICE_LLM_MODEL" })
        #expect(!ModelsRules.voiceWordsIsAControl(facts: older), "an older CLI: the voice takes the work's model")
    }

    @Test func aFirstSetUpTakesTheCLIsRuleAndAReviewKeepsWhatIsSet() {
        var report = PreviewData.models
        report.voiceLlmSeparate = true
        report.voiceLlmRecommended = "qwen3.5-4b"
        // A first set-up: the CLI's pick, as it reports it.
        #expect(ModelsRules.voiceWords(report, voice: PreviewData.voice, firstSetUp: true, workHere: true) == "qwen3.5-4b")
        report.voiceLlmSeparate = false
        report.voiceLlmRecommended = nil
        #expect(ModelsRules.voiceWords(report, voice: PreviewData.voice, firstSetUp: true, workHere: true)
            == ModelsRules.sameAsWork, "no room for two: the work's")
        // Otherwise what is set, never the recommendation.
        report.voiceLlmSeparate = true
        report.voiceLlmRecommended = "qwen3.5-9b"
        #expect(ModelsRules.voiceWords(report, voice: PreviewData.voiceHere, firstSetUp: false, workHere: true)
            == ModelsRules.sameAsWork)
        #expect(ModelsRules.voiceWords(report, voice: PreviewData.voiceOwnLLM, firstSetUp: false, workHere: true)
            == "qwen3.5-4b")
        var remote = PreviewData.voiceHere
        remote.llm = VoiceLLMStatus(source: "remote", model: "gpt-5-mini", url: "https://api.openai.com/v1",
                                    reachable: true, unitInstalled: false)
        #expect(ModelsRules.voiceWords(report, voice: remote, firstSetUp: false, workHere: true) == ModelsRules.keep)
        #expect(ModelsRules.voiceWords(report, voice: PreviewData.voiceHere, firstSetUp: false, workHere: false)
            == ModelsRules.keep, "shared with the work in the cloud: kept")
        // A first set-up is the first run with the voice not here yet.
        #expect(ModelsRules.firstSetUp(.firstRun, voice: PreviewData.voice))
        #expect(!ModelsRules.firstSetUp(.firstRun, voice: PreviewData.voiceHere))
        #expect(!ModelsRules.firstSetUp(.review, voice: PreviewData.voice))
        #expect(VoiceLLMPick.of(ModelsRules.keep) == nil)
        #expect(VoiceLLMPick.of(ModelsRules.sameAsWork) == .shared)
        #expect(VoiceLLMPick.of("qwen3.5-4b") == .own("qwen3.5-4b"))
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
                           .set("AGENTS_ENABLED", ""), .unset("AGENT_SERVER_MODEL")])
    }

    @Test func theVoicesOwnModelIsNeverWrittenOnlyBroughtUp() {
        var report = PreviewData.models
        report.voiceLlmSeparate = true
        let facts = WizardFacts(plan: fresh, config: PreviewData.config)
        // Saving `VOICE_LLM=here` before its server serves would leave the voice with no words.
        #expect(ModelsRules.writes(ModelsChoice(voice: .thisMac, work: .thisMac, wordsModel: "qwen3.5-4b"),
                                   facts: facts, voice: PreviewData.voice, models: report).isEmpty)
        #expect(ModelsRules.writes(ModelsChoice(voice: .thisMac, work: .thisMac, wordsModel: ""),
                                   facts: facts, voice: PreviewData.voice, models: report).isEmpty)
    }

    @Test func aVoiceModelOfItsOwnIsDownloadedThenServedBeforeTheVoice() {
        let facts = WizardFacts(plan: fresh, config: PreviewData.config)
        var report = PreviewData.models
        var voice = PreviewData.voice
        voice.here.modelsReady = true
        let own = LocalIntent(voiceHere: true, workHere: false, model: nil, voiceLLM: .own("qwen3.5-4b"))
        #expect(LocalWork.next(own, models: report, voice: voice, facts: facts, busy: false) == .download("qwen3.5-4b"))
        #expect(LocalStep.download("qwen3.5-4b").row(own) == .voice)
        let index = report.models.firstIndex { $0.name == "qwen3.5-4b" }!
        report.models[index].downloaded = true
        #expect(LocalWork.next(own, models: report, voice: voice, facts: facts, busy: false) == .voiceLLM("qwen3.5-4b"))
        #expect(LocalStep.voiceLLM("qwen3.5-4b").row(own) == .voice)
        var served = PreviewData.voiceOwnLLM
        served.here.reachable = false
        #expect(LocalWork.next(own, models: report, voice: served, facts: facts, busy: false) == .setUpVoice)
        #expect(LocalWork.next(own, models: report, voice: PreviewData.voiceOwnLLM, facts: facts, busy: false) == .done)
    }

    @Test func theWorksModelForTheVoiceIsAskedForOnlyWhenTheWordsComeFromElsewhere() {
        let facts = WizardFacts(plan: fresh, config: PreviewData.config)
        var voice = PreviewData.voice
        voice.here.modelsReady = true
        let shared = LocalIntent(voiceHere: true, workHere: true, model: "qwen3-coder-30b-a3b", voiceLLM: .shared)
        let local = WizardFacts(plan: fresh, config: settings(PreviewData.config, ["AGENT_BACKEND": .string("local")]))
        // Already shared, and the CLI's rule would not give it one of its own: straight on.
        #expect(LocalWork.next(shared, models: PreviewData.models, voice: voice, facts: local, busy: false) == .setUpVoice)
        // A first set-up where the CLI would pick one of its own: shared is asked for first.
        var separate = PreviewData.models
        separate.voiceLlmSeparate = true
        #expect(LocalWork.next(shared, models: separate, voice: voice, facts: local, busy: false) == .voiceLLMShared)
        // Its own model in use: back to the work's.
        #expect(LocalWork.next(shared, models: PreviewData.models, voice: PreviewData.voiceOwnLLM, facts: local,
                               busy: false) == .voiceLLMShared)
    }

    @Test func theWorksServerIsStartedOnItsOwnBesideAVoiceWithItsOwnWords() {
        let local = WizardFacts(plan: fresh, config: settings(PreviewData.config, ["AGENT_BACKEND": .string("local")]))
        var report = PreviewData.models
        report.servers.llm.reachable = false
        report.models = report.models.map { entry in
            var entry = entry
            if entry.name == "qwen3.5-4b" { entry.downloaded = true }
            return entry
        }
        let both = LocalIntent(voiceHere: true, workHere: true, model: "qwen3-coder-30b-a3b",
                               voiceLLM: .own("qwen3.5-4b"))
        #expect(LocalWork.next(both, models: report, voice: PreviewData.voiceOwnLLM, facts: local, busy: false)
            == .startServer)
    }

    @Test func modelsWritesThroughTheVoiceAndConfigCommands() async throws {
        let (model, fake) = Self.model()
        _ = await WizardWrite.apply([.voiceOpenAI, .set("AGENT_BACKEND", "codex"), .set("AGENTS_ENABLED", ""),
                                     .unset("AGENT_SERVER_MODEL")], with: model)
        #expect(Self.written(fake) == [
            ["voice", "use", "openai", "--json"], ["config", "set", "AGENT_BACKEND", "codex"],
            ["config", "set", "AGENTS_ENABLED", ""], ["config", "unset", "AGENT_SERVER_MODEL"],
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
        // An older CLI's voice here with the work in the cloud: its words model is chosen
        // without the default.
        let voiceOnly = LocalIntent(voiceHere: true, workHere: false, model: "gpt-oss-20b")
        let facts = WizardFacts(plan: fresh, config: PreviewData.config.filter { $0.key != "VOICE_LLM_MODEL" })
        #expect(LocalWork.next(voiceOnly, models: PreviewData.models, voice: PreviewData.voice, facts: facts, busy: false)
            == .use("gpt-oss-20b", defaultAgent: false))
        #expect(LocalWork.next(LocalIntent(voiceHere: false, workHere: false, model: nil), models: report,
                               voice: nil, facts: facts, busy: true) == .done, "nothing here")
    }

    @Test func theRunsChoicesWinOverWhatIsSetUp() {
        let wizard = SetupWizard()
        let older = WizardFacts(plan: fresh, config: PreviewData.config.filter { $0.key != "VOICE_LLM_MODEL" })
        let today = LocalIntent.of(wizard, facts: older, voice: PreviewData.voice, models: PreviewData.models)
        #expect(today == LocalIntent(voiceHere: false, workHere: false, model: nil))
        wizard.voicePlace = .thisMac
        wizard.voiceWordsModel = "gemma-4-12b"
        #expect(LocalIntent.of(wizard, facts: older, voice: PreviewData.voice, models: PreviewData.models)
            == LocalIntent(voiceHere: true, workHere: false, model: "gemma-4-12b"))
        // A CLI with a voice LLM: no agent's model for the voice, only its own words.
        let facts = WizardFacts(plan: fresh, config: PreviewData.config)
        wizard.voiceLLM = .own("qwen3.5-4b")
        #expect(LocalIntent.of(wizard, facts: facts, voice: PreviewData.voice, models: PreviewData.models)
            == LocalIntent(voiceHere: true, workHere: false, model: nil, voiceLLM: .own("qwen3.5-4b")))
    }

    // MARK: - The PIN and the phone

    @Test func thePinIsTypedHereOnlyWhenThereIsNone() {
        #expect(PinRules.asksForDigits(PreviewData.pinNone))
        #expect(!PinRules.asksForDigits(PreviewData.pinFile), "a working PIN changes through the sheet")
        #expect(!PinRules.asksForDigits(PreviewData.pinEnvironment))
        #expect(!PinRules.asksForDigits(nil))
        // A PIN file that holds no PIN still shut the enrolment door: only the sheet's yes reopens it.
        let unusable = PinStatus(set: false, source: "file", enrolmentOpen: false, locked: false, lockedUntil: nil)
        #expect(PinWording.kind(unusable) == .unusable)
        #expect(!PinRules.asksForDigits(unusable))
        #expect(PinRules.save(unusable, choice: .now, folded: false, stepDone: false) == .nothing)
        #expect(PinRules.answered(PreviewData.pinNone, choice: .now, first: "482915", second: "482915"))
        #expect(!PinRules.answered(PreviewData.pinNone, choice: .now, first: "482915", second: "48291"))
        #expect(!PinRules.answered(PreviewData.pinNone, choice: .now, first: "123456", second: "123456"), "a run")
        #expect(PinRules.answered(PreviewData.pinNone, choice: .firstCall, first: "", second: ""))
        #expect(PinRules.answered(PreviewData.pinNone, choice: .now, first: "", second: "", folded: true))
        #expect(PinRules.answered(PreviewData.pinFile, choice: .now, first: "", second: ""))
    }

    @Test func continueDecidesThePinFromWhatIsShown() {
        let none = PreviewData.pinNone
        // A first run, nothing settled: the digits, or the first call.
        #expect(PinRules.save(none, choice: .now, folded: false, stepDone: false) == .digits)
        #expect(PinRules.save(none, choice: .firstCall, folded: false, stepDone: false) == .firstCall)
        // Left to the first call before, shown folded: left alone.
        #expect(PinRules.save(none, choice: .firstCall, folded: true, stepDone: true) == .nothing)
        // A review shows it open: digits typed there are a PIN to set, whatever the plan said.
        #expect(PinRules.save(none, choice: .now, folded: false, stepDone: true) == .digits)
        #expect(PinRules.save(none, choice: .firstCall, folded: false, stepDone: true) == .nothing,
                "the first call again writes nothing")
        #expect(PinRules.save(PreviewData.pinFile, choice: .now, folded: false, stepDone: true) == .nothing)
    }

    @Test func aPinTypedInAReviewAlwaysReachesTheClient() async throws {
        // Run Setup Wizard… on a Mac whose PIN was left to the first call: the plan has `pin`
        // done and there is no PIN. Choose It Now, digits typed twice, Continue.
        let (model, fake) = Self.model()
        let decision = PinRules.save(PreviewData.pinNone, choice: .now, folded: false, stepDone: true)
        let outcome = await AboutYouSave.run([], pin: decision, digits: "482915", status: PreviewData.pinNone,
                                             model: model)
        #expect(outcome == .saved)
        let request = try #require(Self.pinSet(fake))
        #expect(request.stdin == Data("482915\n482915\n".utf8))
        // Leaving it to the first call marks the step; the digits never travel then.
        let (other, marks) = Self.model()
        _ = await AboutYouSave.run([.set("OWNER_NAME", "Ada")], pin: .firstCall, digits: "", status: PreviewData.pinNone,
                                   model: other)
        #expect(Self.written(marks) == [["config", "set", "OWNER_NAME", "Ada"], ["setup", "mark", "pin", "--json"]])
    }

    @Test func aPinTypedHereGoesOnStdinNeverArgv() async throws {
        let (model, fake) = Self.model()
        _ = await model.setPin("482915", replace: false)
        let request = try #require(Self.pinSet(fake))
        #expect(!request.arguments.joined().contains("482915"))
        #expect(request.stdin == Data("482915\n482915\n".utf8))
    }

    /// The `pin set --stdin --json` the runner was asked for.
    static func pinSet(_ fake: FakeRunner) -> CommandRequest? {
        fake.requests.first {
            Array($0.arguments.dropFirst(KeryxLocation.fixture.prefixArguments.count)) == ["pin", "set", "--stdin", "--json"]
        }
    }

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
        #expect(CheckRow.step("owner").frame == .aboutYou && CheckRow.step("pin").frame == .aboutYou)
        #expect(CheckRow.step("local").frame == .models && CheckRow.step("migrate").frame == .migrate)
        #expect(CheckRow.step("issues").frame == nil, "the main window's checklist mends it")
        let both = LocalIntent(voiceHere: true, workHere: true, model: "m")
        let voiceOnly = LocalIntent(voiceHere: true, workHere: false, model: "m")
        #expect(LocalStep.downloadSpeech.row(both) == .voice && LocalStep.startServer.row(both) == .work)
        #expect(LocalStep.download("m").row(both) == .work && LocalStep.download("m").row(voiceOnly) == .voice)
    }

    @Test func checkingListsEveryStepThePlanStillHoldsOpen() {
        // Only the owner's details left, as today's CLI holds a blank name: a row of its own.
        var ownerLeft = with(ready, ["owner"], .needed)
        ownerLeft.ready = false
        #expect(CheckRow.rows(ownerLeft, local: false) == [.voice, .work, .phone, .service, .step("owner")])
        #expect(CheckingRules.ending(ownerLeft, phoneLater: false) == .checking)
        #expect(WizardWording.checkRow(.step("owner"), plan: ownerLeft) == "Your Details")
        // The PIN, a migration, and the local models when this run puts something here.
        var several = with(migrating, ["pin"], .needed)
        several = with(several, ["local"], .optional)
        let rows = CheckRow.rows(several, local: true)
        #expect(rows.contains(.step("migrate")) && rows.contains(.step("pin")) && rows.contains(.step("owner")))
        #expect(rows.last == .step("local"))
        #expect(!CheckRow.rows(several, local: false).contains(.step("local")))
        #expect(CheckRow.rows(ready, local: false) == CheckRow.fixed)
    }

    @Test func aRowsWhySaysTitlesNeverKeys() {
        let titles = Wording.titles(PreviewData.config)
        let owner = WizardWording.why(fresh.step("owner"), titles: titles) ?? ""
        #expect(!owner.contains("ALLOWED_CALLERS"), "\(owner)")
        let twilio = SetupStep(id: "phone", title: "Phone", required: true, state: "needed",
                               why: "Twilio credentials: missing TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN.",
                               blockedBy: [], walked: false, checks: [], fix: nil)
        let said = WizardWording.why(twilio, titles: titles) ?? ""
        #expect(!said.contains("TWILIO_ACCOUNT_SID") && !said.contains("TWILIO_AUTH_TOKEN"), "\(said)")
    }

    @Test func aStoppedStepShowsOnlyWhileItIsTheRowsNextStep() {
        let both = LocalIntent(voiceHere: true, workHere: true, model: "gemma-4-12b")
        let a = LocalStep.download("gemma-4-12b")
        // Stopped, and still what the work row needs next: "Stopped", Start Again.
        #expect(CheckingRules.stopped(.work, next: a, intent: both, stopped: [a]) == a)
        #expect(CheckingRules.stopped(.voice, next: a, intent: both, stopped: [a]) == nil, "another row's")
        // The owner chose model B since, or the step is done: A shows nowhere.
        #expect(CheckingRules.stopped(.work, next: .download("qwen3.5-4b"), intent: both, stopped: [a]) == nil)
        #expect(CheckingRules.stopped(.work, next: .done, intent: both, stopped: [a]) == nil)
        #expect(CheckingRules.stopped(.voice, next: .setUpVoice, intent: both, stopped: [.downloadSpeech]) == nil)
    }

    @Test func aNewChoiceOnModelsForgetsWhatWasStopped() {
        let wizard = SetupWizard()
        wizard.choose(voice: .thisMac, work: .thisMac, workModel: "gemma-4-12b", voiceWordsModel: nil)
        wizard.stopped = [.download("gemma-4-12b"), .downloadSpeech]
        // Back and Continue with the same choice keeps the owner's stops.
        wizard.choose(voice: .thisMac, work: .thisMac, workModel: "gemma-4-12b", voiceWordsModel: nil)
        #expect(wizard.stopped.count == 2)
        // A different model, or the voice moved to OpenAI: they belonged to the old choice.
        wizard.choose(voice: .thisMac, work: .thisMac, workModel: "qwen3.5-4b", voiceWordsModel: nil)
        #expect(wizard.stopped.isEmpty)
        wizard.stopped = [.downloadSpeech]
        wizard.choose(voice: .openai, work: .thisMac, workModel: "qwen3.5-4b", voiceWordsModel: nil)
        #expect(wizard.stopped.isEmpty)
    }

    @Test func theWatcherCountsAStopOnlyWhenARunWasCancellingAndHasGone() {
        let cancelling: [String: Downloads.Phase] = ["a": .cancelling, "b": .running(done: 1, total: 2)]
        #expect(StopWatch.downloads(old: cancelling, new: ["b": .running(done: 2, total: 2)]) == [.download("a")])
        #expect(StopWatch.downloads(old: ["a": .running(done: 1, total: 2)], new: [:]).isEmpty, "finished, not stopped")
        #expect(StopWatch.downloads(old: ["a": .cancelling], new: ["a": .cancelling]).isEmpty, "still stopping")
        #expect(StopWatch.downloads(old: ["a": .failed("x")], new: [:]).isEmpty)
        #expect(StopWatch.stopped(was: true, now: true))
        #expect(!StopWatch.stopped(was: false, now: true))
        #expect(!StopWatch.stopped(was: true, now: false))
        #expect(StopWatch.server(chained: true) == .setUpVoice && StopWatch.server(chained: false) == .startServer)
    }

    @Test func whatTheOwnerStoppedNeverStartsByItself() {
        let step = LocalStep.download("gemma-4-12b")
        #expect(CheckingRules.startsByItself(step, mode: .firstRun, approved: false, stopped: []))
        #expect(!CheckingRules.startsByItself(step, mode: .firstRun, approved: false, stopped: [step]))
        #expect(CheckingRules.startsByItself(.downloadSpeech, mode: .firstRun, approved: false, stopped: [step]))
        // A review starts nothing without the owner's click.
        #expect(!CheckingRules.startsByItself(step, mode: .review, approved: false, stopped: []))
        #expect(CheckingRules.startsByItself(step, mode: .review, approved: true, stopped: []))
        #expect(!CheckingRules.startsByItself(step, mode: .review, approved: true, stopped: [step]))
        let wizard = SetupWizard()
        wizard.stopped = [step]
        wizard.localApproved = true
        wizard.start(WizardOpening(mode: .firstRun))
        #expect(wizard.stopped.isEmpty && !wizard.localApproved, "a new run forgets them")
    }

    @Test func readyNeedsTheNumberToRingThisMac() {
        #expect(CheckingRules.ending(ready, phoneLater: false, pointed: true) == .ready)
        #expect(CheckingRules.ending(ready, phoneLater: false, pointed: false) == .notPointed)
        #expect(CheckingRules.ending(ready, phoneLater: false, pointed: nil) == .ready, "not asked: not held back")
        #expect(WizardWording.notPointed(number: "+15550001111")
            == "Everything is set up, but your number still rings its old address. Point it at this Mac "
            + "from Phone in the sidebar, then ring +1 555 000 1111.")
    }

    // MARK: - Words

    @Test func welcomeSaysHowFarThereIsToGo() {
        #expect(WizardWording.welcome(mode: .firstRun, resuming: false, left: 6, frames: 6, assistant: "Lyra")
            .hasSuffix("Six short steps and you're set."))
        #expect(WizardWording.welcome(mode: .firstRun, resuming: false, left: 6, frames: 3, assistant: "Lyra")
            .hasSuffix("Three short steps and you're set."))
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
                          "Keryx stops while they move, and starts again after.",
                          "The Jarvis folder is renamed aside. Nothing is deleted."])
        #expect(lines.allSatisfy { !$0.contains("/") })
        #expect(WizardWording.moveSummary(PreviewData.migrationPlanNothing) == ["Nothing is left to move."])
        #expect(WizardPaths.short("/Users/you/.jarvis/pin") == "~/.jarvis/pin")
    }

    @Test func readyNamesTheNumberAndThePinStillToChoose() {
        #expect(WizardWording.ready("Lyra", number: "+15550100100", pinSet: true)
            == "Lyra can take your first call. Ring +1 555 010 0100 from your phone. Lyra will say hello and ask a little about you.")
        #expect(WizardWording.ready("Lyra", number: nil, pinSet: false).hasSuffix("and ask you to choose a PIN."))
    }

    @Test func everyFrameAndStateIsInTheCatalog() {
        let ids = Set(ScreenCatalog.entries().map(\.id))
        for id in ["wizard-welcome", "wizard-welcome-resuming", "wizard-welcome-review", "wizard-move-files",
                   "wizard-move-files-conflict", "wizard-move-files-moving", "wizard-move-files-failed",
                   "wizard-about-you", "wizard-about-you-review", "wizard-models", "wizard-models-this-mac-voice",
                   "wizard-models-this-mac-both", "wizard-models-voice-model", "wizard-models-elsewhere",
                   "wizard-about-you-pin-set", "wizard-about-you-pin-started-with", "wizard-loading",
                   "wizard-needs-updating", "wizard-not-pointed", "wizard-checking-stopped", "wizard-checking-review",
                   "wizard-checking-details-left", "wizard-phone-installing-tunnel", "onboarding-migration-moving",
                   "wizard-phone", "wizard-checking", "wizard-checking-failed", "wizard-ready",
                   "wizard-nearly-ready", "wizard-profile", "wizard-projects-draft"] {
            #expect(ids.contains(id), "missing \(id)")
        }
    }

    // MARK: - Words the owner reads

    @Test func numbersAreShownGroupedWhereTheGroupingIsCertain() {
        #expect(PhoneDisplay.format("+15550001111") == "+1 555 000 1111")
        #expect(PhoneDisplay.format("+447700900123") == "+44 7700 900123")
        #expect(PhoneDisplay.format("+4930123456") == "+4930123456", "unsure: as saved")
        #expect(CallerNumbers.normalised(PhoneDisplay.format("+15550001111")) == "+15550001111", "and back")
    }

    @Test func theChecklistSpeaksTheWizardsWords() throws {
        let doctor = PreviewData.doctor
        let service = try #require(doctor.checks.first { $0.name == "service manager" })
        #expect(ChecklistWording.title(service) == "Runs in the background")
        var failing = service
        failing.state = "failed"
        #expect(ChecklistWording.detail(failing) != "Keryx isn't answering calls yet",
                "a real failure keeps what it says")
        #expect(ChecklistWording.detail(service) == "Keryx isn't answering calls yet")
        let twilio = try #require(doctor.checks.first { $0.name == "Twilio credentials" })
        #expect(ChecklistWording.title(twilio) == "Twilio Account" && ChecklistWording.detail(twilio) == "Not connected")
        let host = try #require(doctor.checks.first { $0.name == "PUBLIC_HOST" })
        #expect(ChecklistWording.title(host) == "Address for Calls" && ChecklistWording.detail(host) == "Not set")
        let tunnel = try #require(doctor.checks.first { $0.name == "tunnel" })
        #expect(ChecklistWording.title(tunnel) == "Cloudflare Tunnel" && ChecklistWording.detail(tunnel) == "Not installed")
        #expect(doctor.checks.allSatisfy { !ChecklistWording.detail($0).contains("this machine") })
        #expect(FixAction("phone:tunnel")?.action(SetupContext(phoneTunnel: PreviewData.phoneTunnel)).title == "Fix…")
    }

    @Test func theSheetsSayWhatAPersonReads() {
        #expect(ProfileSheet.note("What you say here is sent to the realtime provider at the top of every call.")
            == "What you say here is sent to the voice provider at the top of every call.")
        #expect(MemoryText.lines("# What Lyra knows\n\n## Standing facts\n\n- Works nights.\nPlain.")
            == [.heading("What Lyra knows", level: 1), .heading("Standing facts", level: 2), .item("Works nights."),
                .plain("Plain.")])
        #expect(ProjectContextSheet.progress("[tool] Read README.md") == "Reading README.md")
        #expect(ProjectContextSheet.progress("[tool] Grep TODO") == "Grep TODO")
        #expect(MigrationSections.said(StreamFailure("The migration stopped before it finished. Migrating again picks "
            + "up where it stopped.", detail: "x")).message
            == "The move stopped before it finished. Try Again picks up where it stopped.")
        #expect(WizardWording.signedIn("stored subscription login", agent: "Claude") == "Signed in on your Claude plan")
        #expect(WizardWording.signedIn("ANTHROPIC_API_KEY (API key)", agent: "Claude") == "Signed in with an API key")
        #expect(PhoneSteps.tunnelName("cloudflared") == "Cloudflare Tunnel")
        #expect(PhoneSteps.shortURL("https://demo.twilio.com/welcome/voice/") == "demo.twilio.com/welcome/voice/")
    }

    @Test func phoneNumbersInThePickerAreGroupedToo() {
        #expect(PhoneWording.label(PreviewData.phoneNumbers.numbers[0]) == "Office · +1 555 000 1111")
        #expect(WizardWording.sharedWords(tight: false, assistant: "Lyra")
            == "Lyra thinks with the work model, so answers come a little slower.")
        #expect(FixAction("service:install")?.action(SetupContext()).title == "Start Keryx")
    }

    @Test func aDisabledContinueSaysWhy() {
        let state = ModelsFrame.FrameState(voice: .openai, work: .claude, workModel: nil, wordsModel: nil,
                                           voiceAnswered: false, workAnswered: false, voiceFolded: false,
                                           workFolded: false)
        #expect(ModelsFrame.waiting(state) == "Add a key and sign in to continue")
        var models = state
        models.voice = .thisMac
        models.work = .thisMac
        #expect(ModelsFrame.waiting(models) == "Choose a model to continue", "said once")
    }

    // MARK: - Helpers

    /// A model over the real client, its commands answered by a fake runner.
    static func model() -> (AppModel, FakeRunner) {
        let fake = FakeRunner { request in
            let words = Array(request.arguments.dropFirst(KeryxLocation.fixture.prefixArguments.count))
            let body: String = switch words.first {
            case "voice": #"{"saved": {}, "restart_needed": true}"#
            case "setup": #"{"id": "pin", "walked": true}"#
            case "pin": #"{"set": true, "replaced": false, "restart_needed": true}"#
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
