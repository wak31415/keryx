import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// The Setup Assistant's walk through `keryx setup plan`: its pages and their order, where
/// it opens, the buttons each step offers, its words, and whether it opens at launch.
@MainActor @Suite struct SetupAssistantTests {
    let fresh = PreviewData.setupPlan
    let partway = PreviewData.setupPlanPartway
    let ready = PreviewData.setupPlanReady

    func step(_ plan: SetupPlan, _ id: String) -> SetupStep {
        guard let step = plan.step(id) else { fatalError("no step \(id)") }
        return step
    }

    /// `plan` with `id`'s state replaced.
    func with(_ plan: SetupPlan, _ id: String, state: SetupStepState, walked: Bool? = nil) -> SetupPlan {
        var plan = plan
        if let index = plan.steps.firstIndex(where: { $0.id == id }) {
            plan.steps[index].state = state.rawValue
            if let walked { plan.steps[index].walked = walked }
        }
        return plan
    }

    /// `plan` with no step carrying a `fix`: what an older CLI says, and the step's own
    /// buttons alone.
    func withoutFixes(_ plan: SetupPlan) -> SetupPlan {
        var plan = plan
        for index in plan.steps.indices { plan.steps[index].fix = nil }
        return plan
    }

    // MARK: - Pages

    @Test func theRequiredStepsComeFirstThenTheOptionalOnesThenTheLastPage() {
        let pages = SetupGuide.pages(fresh)
        #expect(pages.first == .step("voice"))
        #expect(pages.last == .finish)
        #expect(pages.count == fresh.steps.count + 1)
        let required = pages.prefix(6)
        #expect(Array(required) == ["voice", "agents", "owner", "pin", "phone", "service"].map { .step($0) })

        // A required step the plan lists among the optional ones (`local`, when the voice
        // runs on it) is walked with the required ones.
        var plan = fresh
        let local = plan.steps.firstIndex { $0.id == "local" }!
        plan.steps[local].required = true
        #expect(SetupGuide.pages(plan)[6] == .step("local"))
    }

    @Test func itOpensAtTheFirstStepThatCanBeDoneNow() {
        #expect(SetupGuide.firstPage(fresh) == .step("voice"))
        #expect(SetupGuide.firstPage(partway) == .step("voice"))
        // Not a step that waits, while one that does not is left.
        let onlyPhoneLeft = with(with(with(with(fresh, "voice", state: .done), "agents", state: .done),
                                      "owner", state: .done), "pin", state: .done)
        #expect(SetupGuide.firstPage(onlyPhoneLeft) == .step("phone"), "both left wait: the first of them")
        let phoneNeeded = with(onlyPhoneLeft, "phone", state: .needed)
        #expect(SetupGuide.firstPage(phoneNeeded) == .step("phone"))
        // A failed required step counts as one to do now.
        #expect(SetupGuide.firstPage(with(onlyPhoneLeft, "phone", state: .failed)) == .step("phone"))
    }

    @Test func itOpensAtTheLastPageOnceReady() {
        #expect(SetupGuide.firstPage(ready) == .finish)
    }

    @Test func aPageTheNewPlanLacksFallsBackToWhereItOpens() {
        #expect(SetupGuide.resolve(nil, in: fresh) == .step("voice"))
        #expect(SetupGuide.resolve(.step("migrate"), in: fresh) == .step("voice"), "the migration has run")
        #expect(SetupGuide.resolve(.step("pin"), in: fresh) == .step("pin"))
        #expect(SetupGuide.resolve(.finish, in: fresh) == .finish)
    }

    @Test func backAndContinueWalkThePagesInOrder() {
        #expect(SetupGuide.page(before: .step("voice"), in: fresh) == nil)
        #expect(SetupGuide.page(after: .step("voice"), in: fresh) == .step("agents"))
        #expect(SetupGuide.page(after: .step("service"), in: fresh) == .step("local"))
        #expect(SetupGuide.page(after: .step("projects"), in: fresh) == .finish)
        #expect(SetupGuide.page(after: .finish, in: fresh) == nil)
        #expect(SetupGuide.page(before: .finish, in: fresh) == .step("projects"))
    }

    @Test func requiredStepsAreCountedAndTheRestAreOptional() {
        #expect(SetupGuide.position(of: step(fresh, "voice"), in: fresh) == "Step 1 of 6")
        #expect(SetupGuide.position(of: step(fresh, "service"), in: fresh) == "Step 6 of 6")
        #expect(SetupGuide.position(of: step(fresh, "issues"), in: fresh) == "Optional")
    }

    @Test func aStepWaitsOnlyForBlockersNotYetDone() {
        let blockers = SetupGuide.blockers(of: step(fresh, "service"), in: fresh).map(\.id)
        #expect(blockers == ["voice", "agents", "phone"], "in the plan's order")
        #expect(SetupGuide.blockers(of: step(partway, "phone"), in: partway).isEmpty, "the owner is done")
    }

    // MARK: - Skipping

    @Test func onlyAnOptionalStepStillOpenCanBeSkipped() {
        let context = SetupContext()
        #expect(SetupGuide.skip(step(fresh, "issues"), context: context)
            == SetupAction(title: "Skip", effect: .mark("issues")))
        #expect(SetupGuide.skip(step(fresh, "google"), context: context) == nil, "done")
        #expect(SetupGuide.skip(step(partway, "plugins"), context: context) == nil,
                "failed: walking it would not settle it")
        #expect(SetupGuide.skip(step(fresh, "voice"), context: context) == nil, "required")
        #expect(SetupGuide.skip(step(fresh, "service"), context: context) == nil, "required, waiting")
    }

    @Test func thePinCanBeLeftToTheFirstCall() {
        let action = SetupGuide.skip(step(fresh, "pin"), context: SetupContext(pin: PreviewData.pinNone))
        #expect(action == SetupAction(title: "Decide on the First Call", effect: .mark("pin")))
        #expect(SetupGuide.skip(step(partway, "pin"), context: SetupContext()) == nil, "set already")
        #expect(SetupGuide.skip(step(fresh, "pin"), context: SetupContext()) == nil, "the PIN's status not read yet")
        // A PIN in the environment is not the app's to set or defer.
        #expect(SetupGuide.skip(step(fresh, "pin"), context: SetupContext(pin: PreviewData.pinEnvironment)) == nil)
    }

    // MARK: - Buttons

    func actions(_ plan: SetupPlan, _ id: String, _ context: SetupContext = SetupContext()) -> SetupActions {
        SetupGuide.actions(step(plan, id), in: plan, context: context)
    }

    var keyUnset: [ConfigRow] {
        PreviewData.config.map { row in
            var row = row
            if row.key == "OPENAI_API_KEY" { row.set = false }
            return row
        }
    }

    @Test func theVoiceAsksForAnOpenAIKeyHereOrSendsToTheVoiceSection() {
        let openai = actions(fresh, "voice", SetupContext(config: keyUnset, voice: PreviewData.voice))
        #expect(openai.primary == SetupAction(title: "Add OpenAI API Key…",
                                              effect: .secret(key: "OPENAI_API_KEY", title: "OpenAI API key",
                                                              replacing: false)))
        #expect(openai.others.map(\.effect) == [.request(.show(.voice))])
        let saved = actions(withoutFixes(fresh), "voice", SetupContext(config: PreviewData.config, voice: PreviewData.voice))
        #expect(saved.primary?.title == "Change OpenAI API Key…")

        let older = withoutFixes(fresh)
        let here = actions(older, "voice", SetupContext(voice: PreviewData.voiceHere))
        #expect(here.primary?.effect == .request(.setUpVoiceHere))
        let server = actions(older, "voice", SetupContext(voice: PreviewData.voiceServer))
        #expect(server.primary?.effect == .request(.show(.voice)))
    }

    @Test func theAgentsSignInToTheDefaultAgent() {
        let claude = actions(partway, "agents", SetupContext(config: PreviewData.config))
        #expect(claude.primary == SetupAction(title: "Sign In to Claude…", effect: .request(.signIn(.claude))))
        #expect(claude.others.first?.effect == .secret(key: "ANTHROPIC_API_KEY", title: "Anthropic API key",
                                                       replacing: false))
        // Without the CLI's fix, by the default agent the settings name.
        let older = withoutFixes(partway)
        #expect(actions(older, "agents", SetupContext(config: backend("codex"))).primary?.effect
            == .request(.signIn(.codex)))
        #expect(actions(older, "agents", SetupContext(config: backend("local"))).primary?.effect
            == .request(.show(.models)))
        #expect(actions(older, "agents", SetupContext(config: backend("gemini"))).primary?.effect
            == .request(.configure(tab: "agents")))
        #expect(SetupGuide.settings(step(partway, "agents")) == ["AGENT_BACKEND"])
    }

    func backend(_ name: String) -> [ConfigRow] {
        PreviewData.config.map { row in
            var row = row
            if row.key == "AGENT_BACKEND" { row.value = .string(name) }
            return row
        }
    }

    @Test func theOwnerIsAskedInPlace() {
        let owner = actions(fresh, "owner")
        #expect(owner.primary == nil && owner.others.isEmpty)
        #expect(SetupGuide.settings(step(fresh, "owner")) == ["OWNER_NAME", "ALLOWED_CALLERS"])
        #expect(SetupGuide.settings(step(fresh, "phone")).isEmpty)
    }

    @Test func thePinOpensThePinSheetUnlessTheEnvironmentHoldsIt() {
        #expect(actions(fresh, "pin", SetupContext(pin: PreviewData.pinNone)).primary
            == SetupAction(title: "Set PIN…", effect: .request(.setPin)))
        #expect(actions(partway, "pin", SetupContext(pin: PreviewData.pinFile)).primary?.title == "Change PIN…")
        #expect(actions(fresh, "pin", SetupContext(pin: PreviewData.pinEnvironment)).primary == nil)
    }

    @Test func aStepThatWaitsLeadsToWhatItWaitsFor() {
        let service = actions(fresh, "service", SetupContext(service: PreviewData.service))
        #expect(service.primary == SetupAction(title: "Go to Voice", effect: .go(.step("voice"))))
        #expect(service.others.map(\.effect) == [.installService], "its own button follows")
        let phone = actions(fresh, "phone")
        #expect(phone.primary?.effect == .go(.step("owner")))
        #expect(phone.others.map(\.effect) == [.request(.show(.phone))])
    }

    @Test func theServiceIsInstalledHereAndOnceDoneOffersNothing() {
        var units = PreviewData.service
        units.units.agent.installed = false
        let plan = with(fresh, "service", state: .needed)
        #expect(actions(plan, "service", SetupContext(service: units)).primary
            == SetupAction(title: "Install Service…", effect: .installService))
        units.units.agent.installed = true
        #expect(actions(plan, "service", SetupContext(service: units)).primary?.title == "Start Service…")
        let done = actions(ready, "service")
        #expect(done.primary == nil && done.others.isEmpty)
    }

    @Test func theLocalModelsChooseAModelThenStartItsServer() {
        var report = PreviewData.models
        #expect(actions(fresh, "local", SetupContext(models: report)).primary?.effect == .request(.show(.models)),
                "served already")
        report.servers.llm.reachable = false
        let stopped = actions(fresh, "local", SetupContext(models: report))
        #expect(stopped.primary == SetupAction(title: "Start Model Server", effect: .startModelServer))
        #expect(stopped.others.map(\.effect) == [.request(.show(.models))])
        report.servers.llm.model = nil
        #expect(actions(fresh, "local", SetupContext(models: report)).primary
            == SetupAction(title: "Choose a Model", effect: .request(.show(.models))))
        report.servers.llm.model = "not-downloaded"
        #expect(actions(fresh, "local", SetupContext(models: report)).primary?.title == "Choose a Model")
        #expect(actions(fresh, "local").primary?.title == "Choose a Model", "before the models are read")
    }

    @Test func theRestDeepLinkOrOpenTheTerminal() {
        #expect(actions(fresh, "google").primary?.effect == .request(.connect(integration: "google")))
        #expect(actions(withoutFixes(fresh), "plugins").primary?.effect == .request(.show(.plugins)))
        #expect(actions(fresh, "issues").primary?.effect == .request(.connect(integration: "github")))
        #expect(actions(fresh, "profile").primary?.effect == .terminal)
        #expect(actions(withoutFixes(fresh), "projects").primary?.effect == .terminal)
        var plan = withoutFixes(fresh)
        plan.steps[0].id = "migrate"
        #expect(actions(plan, "migrate").primary?.effect == .request(.show(.overview)))
        plan.steps[0].id = "something-new"
        #expect(actions(plan, "something-new").primary?.effect == .terminal)
    }

    @Test func theCLIsFixLeadsAndTheStepsOwnButtonsFollow() {
        // The plugins step: Gmail's sign-in, which Email answers waits on, then Plugins.
        let plugins = actions(partway, "plugins")
        #expect(plugins.primary == SetupAction(title: "Sign In to Gmail…", effect: .request(.signIn(.gmail))))
        #expect(plugins.others.map(\.effect) == [.request(.show(.plugins))])
        // A fix that is what the step's own button does is offered once.
        let voice = actions(fresh, "voice", SetupContext(config: keyUnset, voice: PreviewData.voice))
        #expect(voice.others.map(\.effect) == [.request(.show(.voice))])
        // The phone: Twilio's account sheet, then Phone.
        let phone = actions(partway, "phone")
        #expect(phone.primary == SetupAction(title: "Connect Twilio…", effect: .request(.phoneAccount)))
        #expect(phone.others.map(\.effect) == [.request(.show(.phone))])
        // A setting the page asks for in place is not a button too.
        #expect(actions(fresh, "owner").primary == nil)
        // The projects folder: its setting, then the terminal for the briefs.
        #expect(actions(fresh, "projects").primary?.effect == .request(.setting(key: "PROJECTS_ROOT")))
        // Before the settings are read, the check's own title names the key.
        #expect(actions(fresh, "voice").primary?.title == "Add OpenAI API Key…")
    }

    @Test func aStepThatWaitsShowsItsOwnButtonsDisabled() {
        let service = actions(fresh, "service", SetupContext(service: PreviewData.service))
        #expect(service.waiting)
        #expect(!actions(fresh, "voice").waiting)
    }

    // MARK: - Words

    @Test func everyStateHasItsSymbolAndLabel() {
        #expect(SetupWording.symbol(.done) == "checkmark.circle.fill")
        #expect(SetupWording.symbol(.blocked) == "clock")
        #expect(SetupWording.symbol(nil) == "circle.dashed")
        let symbols = Set(SetupStepState.allCases.map { SetupWording.symbol($0) })
        #expect(symbols.count == SetupStepState.allCases.count, "each state looks different")
        #expect(SetupWording.label(.failed) == "Not working")
        #expect(SetupWording.label(step(fresh, "voice").kind) == "Needed")
    }

    @Test func everyKnownStepSaysWhatItIsFor() {
        for id in ["migrate", "voice", "agents", "owner", "pin", "phone", "service", "local", "google", "plugins",
                   "issues", "profile", "projects"] {
            #expect(SetupWording.about(id, assistant: "Lyra") != nil, "\(id)")
            #expect(SetupWording.pageSymbol(id) != "checklist", "\(id)")
        }
        #expect(SetupWording.about("voice", assistant: "Lyra")?.contains("Lyra") == true)
        #expect(SetupWording.about("voice", assistant: nil)?.contains("the assistant") == true)
        #expect(SetupWording.about("projects", assistant: nil)?.hasPrefix("The assistant") == true)
        #expect(SetupWording.about("unknown", assistant: nil) == nil)
    }

    @Test func anOptionalStepsOfferIsNotSaidTwice() {
        #expect(SetupWording.why(step(fresh, "local")) == nil, "Optional: …")
        #expect(SetupWording.why(step(fresh, "issues")) == nil, "Off until you choose: …")
        #expect(SetupWording.why(step(fresh, "profile")) == "Memory: nothing remembered yet.")
        #expect(SetupWording.why(step(fresh, "voice")) == "OpenAI API key: not set.")
        #expect(SetupWording.why(step(ready, "voice")) == nil)
    }

    @Test func theLastPageSaysWhoTakesTheCallAndWhatIsLeft() {
        #expect(SetupWording.finishTitle(ready) == "You're Ready")
        #expect(SetupWording.finishMessage(ready, assistant: "Lyra", number: "+15550100")
            == "Lyra can take your first call. Ring +15550100 from one of your phones.")
        #expect(SetupWording.finishMessage(ready, assistant: nil, number: nil)
            == "The assistant can take your first call. Ring your Twilio number from one of your phones.")
        #expect(SetupWording.finishList(ready).map(\.id) == ["issues", "profile", "projects"])

        #expect(SetupWording.finishTitle(partway) == "Not Ready Yet")
        #expect(SetupWording.finishMessage(partway, assistant: "Lyra", number: nil)
            == "4 required steps are left before the first call.")
        #expect(SetupWording.finishList(partway).map(\.id) == ["voice", "agents", "phone", "service"])
        var one = partway
        one.steps = one.steps.filter { $0.id != "agents" && $0.id != "phone" && $0.id != "service" }
        #expect(SetupWording.finishMessage(one, assistant: nil, number: nil)
            == "1 required step is left before the first call.")
    }

    @Test func theContextReadsTheDefaultAgentAndTheNumber() {
        #expect(SetupContext(config: PreviewData.config).defaultAgent == "claude")
        #expect(SetupContext(config: PreviewData.config).twilioNumber == nil)
        #expect(SetupContext().defaultAgent == nil)
    }

    // MARK: - Opening at launch

    @Test func itOpensAtLaunchOnlyWhileNotReadyAndNotTurnedOff() {
        #expect(SetupLaunch.shouldOpen(plan: fresh, hidden: false, alreadyOpened: false))
        #expect(!SetupLaunch.shouldOpen(plan: ready, hidden: false, alreadyOpened: false))
        #expect(!SetupLaunch.shouldOpen(plan: fresh, hidden: true, alreadyOpened: false))
        #expect(!SetupLaunch.shouldOpen(plan: fresh, hidden: false, alreadyOpened: true))
        #expect(!SetupLaunch.shouldOpen(plan: nil, hidden: false, alreadyOpened: false), "no plan, no window")
    }

    @Test func turningItOffIsRememberedInTheAppsDefaults() {
        let defaults = UserDefaults(suiteName: "SetupAssistantTests-\(UUID().uuidString)")!
        #expect(!SetupLaunch.isHidden(in: defaults))
        defaults.set(true, forKey: SetupLaunch.hiddenKey)
        #expect(SetupLaunch.isHidden(in: defaults))
    }

    // MARK: - The menu

    @Test func theMenusItemOpensTheAssistant() {
        var opened = false
        let actions = MenuActions(openWindow: {}, openSession: { _ in }, quit: {}, openSetup: { opened = true })
        actions.openSetup()
        #expect(opened)
        #expect(KeryxWindowID.setup == "setup")
        #expect(KeryxWindowID.main == "main")
    }
}
