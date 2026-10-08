import Foundation
import KeryxKit

/// A page of the Setup Assistant: one step of the plan, by its id, or the last page.
package enum SetupPage: Hashable, Sendable {
    case step(String)
    case finish
}

/// What a button in the Setup Assistant does.
struct SetupAction: Equatable, Identifiable, Sendable {
    enum Effect: Equatable, Sendable {
        /// Asks the main window (`AppModel.request`) and brings it forward.
        case request(AppRequest)
        /// The secret sheet for `key`, here in the assistant.
        case secret(key: String, title: String, replacing: Bool)
        /// `keryx setup mark STEP`: an optional step skipped, or the PIN left to the first call.
        case mark(String)
        /// `keryx service install`, after a confirmation.
        case installService
        /// `keryx models start`, followed here.
        case startModelServer
        /// `keryx voice download`, followed on Local Models.
        case downloadVoiceModels
        /// A page in the browser: one the app trusts (`FixAction.tunnelPage`).
        case openURL(URL)
        /// Another page of the assistant.
        case go(SetupPage)
        /// `keryx setup` in Terminal, for the sections the app cannot walk yet.
        case terminal
    }

    var title: String
    var effect: Effect

    var id: String { title }
}

/// A step's buttons: the one that settles it, and the others beside it. `waiting`: the step
/// waits for another, so its own buttons are shown but not yet enabled — the primary is
/// the way to what it waits for.
struct SetupActions: Equatable, Sendable {
    var primary: SetupAction?
    var others: [SetupAction] = []
    var waiting = false
}

/// What the assistant knows beside the plan, to say which button settles a step: the
/// settings, the voice, the local models, the PIN and the units.
struct SetupContext: Sendable {
    var config: [ConfigRow]?
    var voice: VoiceStatus?
    var models: ModelsReport?
    var pin: PinStatus?
    var service: ServiceReport?
    var phoneTunnel: PhoneTunnel?
    var integrations: IntegrationsReport?
    var plugins: PluginsReport?

    @MainActor init(_ model: AppModel) {
        self.init(config: model.config.value, voice: model.voice.value, models: model.models.value,
                  pin: model.pin.value, service: model.service.value, phoneTunnel: model.phoneTunnel.value,
                  integrations: model.integrations.value, plugins: model.plugins.value)
    }

    init(config: [ConfigRow]? = nil, voice: VoiceStatus? = nil, models: ModelsReport? = nil,
         pin: PinStatus? = nil, service: ServiceReport? = nil, phoneTunnel: PhoneTunnel? = nil,
         integrations: IntegrationsReport? = nil, plugins: PluginsReport? = nil) {
        self.config = config
        self.voice = voice
        self.models = models
        self.pin = pin
        self.service = service
        self.phoneTunnel = phoneTunnel
        self.integrations = integrations
        self.plugins = plugins
    }

    /// cloudflared's download page, while no tunnel program is installed.
    var tunnelDownload: URL? { FixAction.tunnelPage(phoneTunnel?.installUrl) }

    /// The model this Mac's model server is serving when it runs one other than the model
    /// chosen (`LLM_SERVER_MODEL`), by its title; `nil` when it isn't running, says nothing,
    /// or serves the one chosen.
    var modelServerServesAnother: String? {
        guard let llm = models?.servers.llm, llm.reachable, let served = llm.servedModel, !served.isEmpty,
              let chosen = llm.model, served != chosen else { return nil }
        return models?.models.first { $0.name == served }?.title ?? served
    }

    func pluginTitle(_ name: String) -> String? { plugins?.plugin(name)?.title }

    func integrationTitle(_ id: String) -> String? { integrations?.integration(id)?.title }

    func row(_ key: String) -> ConfigRow? { config?.first { $0.key == key } }

    /// `AGENT_BACKEND`: the agent a task runs on when nobody names one.
    var defaultAgent: String? {
        guard case let .string(name)? = row("AGENT_BACKEND")?.value, !name.isEmpty else { return nil }
        return name
    }

    /// The number the owner rings (`TWILIO_NUMBER`), when one is saved.
    var twilioNumber: String? {
        guard case let .string(number)? = row("TWILIO_NUMBER")?.value, !number.isEmpty else { return nil }
        return number
    }
}

/// How the Setup Assistant walks `keryx setup plan`: the order of its pages, where it
/// opens, and the buttons each step offers. It decides nothing the plan decides — a step's
/// state, whether it waits, whether the whole is ready are the CLI's.
enum SetupGuide {
    // MARK: - Pages

    /// The required steps in the plan's order, then the optional ones, then the last page:
    /// the order the list shows them in, and Back and Continue walk.
    static func pages(_ plan: SetupPlan) -> [SetupPage] {
        let required = plan.steps.filter(\.required)
        let optional = plan.steps.filter { !$0.required }
        return (required + optional).map { .step($0.id) } + [.finish]
    }

    /// Where the assistant opens: the last page once the plan is ready; else the first
    /// required step that can be done now, else the first required step not done, else the
    /// first optional step left open, else the last page.
    static func firstPage(_ plan: SetupPlan) -> SetupPage {
        if plan.ready { return .finish }
        let required = plan.steps.filter(\.required)
        if let step = required.first(where: { $0.kind == .needed || $0.kind == .failed })
            ?? required.first(where: { $0.kind != .done })
            ?? plan.steps.first(where: { !$0.required && $0.kind != .done }) {
            return .step(step.id)
        }
        return .finish
    }

    /// `page` while the plan still has it; otherwise (a step gone, as `migrate` goes once
    /// it has run, or nothing chosen yet) where the assistant opens.
    static func resolve(_ page: SetupPage?, in plan: SetupPlan) -> SetupPage {
        switch page {
        case .finish?: .finish
        case let .step(id)? where plan.step(id) != nil: .step(id)
        default: firstPage(plan)
        }
    }

    static func page(after page: SetupPage, in plan: SetupPlan) -> SetupPage? {
        let pages = pages(plan)
        guard let index = pages.firstIndex(of: page), index + 1 < pages.count else { return nil }
        return pages[index + 1]
    }

    static func page(before page: SetupPage, in plan: SetupPlan) -> SetupPage? {
        let pages = pages(plan)
        guard let index = pages.firstIndex(of: page), index > 0 else { return nil }
        return pages[index - 1]
    }

    /// "Step 3 of 7" for a required step; "Optional" for the rest.
    static func position(of step: SetupStep, in plan: SetupPlan) -> String {
        let required = plan.steps.filter(\.required)
        guard step.required, let index = required.firstIndex(where: { $0.id == step.id }) else { return "Optional" }
        return "Step \(index + 1) of \(required.count)"
    }

    /// The steps `step` waits for that are not done yet, in the plan's order.
    static func blockers(of step: SetupStep, in plan: SetupPlan) -> [SetupStep] {
        plan.steps.filter { step.blockedBy.contains($0.id) && $0.kind != .done }
    }

    // MARK: - Buttons

    /// The button beside Continue that walks past a step without doing it: Skip for an
    /// optional step still open, and for the PIN, leaving it to the first call. `nil` for
    /// everything else, which only its own checks can settle.
    static func skip(_ step: SetupStep, context: SetupContext) -> SetupAction? {
        guard step.kind != .done else { return nil }
        if step.id == "pin" {
            // Only once the PIN's status is read: from the environment it is not the app's
            // to defer, and the button must not flash before that is known.
            guard step.kind == .needed, let pin = context.pin, pin.place != .environment else { return nil }
            return SetupAction(title: "Decide on the First Call", effect: .mark("pin"))
        }
        guard !step.required, step.kind == .optional else { return nil }
        return SetupAction(title: "Skip", effect: .mark(step.id))
    }

    /// The step's buttons. What the CLI says would mend it (`fix`) leads, then the step's own
    /// buttons beside it. A step that waits leads with going to what it waits for, and its
    /// own buttons are shown but disabled until then. A step that is done keeps its own
    /// buttons, to change what settled it.
    static func actions(_ step: SetupStep, in plan: SetupPlan, context: SetupContext) -> SetupActions {
        var actions = own(step, context: context)
        if step.kind != .done, step.kind != .blocked, let fix = fixAction(step, context: context) {
            let rest = ([actions.primary].compactMap { $0 } + actions.others)
                .filter { $0.effect != fix.effect && $0.title != fix.title }
            actions = SetupActions(primary: fix, others: rest)
        }
        if step.kind == .blocked, let first = blockers(of: step, in: plan).first {
            let go = SetupAction(title: "Go to \(Wording.titleCase(first.title))", effect: .go(.step(first.id)))
            actions = SetupActions(primary: go, others: [actions.primary].compactMap { $0 } + actions.others,
                                   waiting: true)
        }
        return actions
    }

    /// The CLI's fix for the step, as a button — not a setting the page already asks for in
    /// place (the owner's name and numbers, the default agent).
    static func fixAction(_ step: SetupStep, context: SetupContext) -> SetupAction? {
        guard let fix = FixAction(step.fix) else { return nil }
        if case let .setting(key) = fix, settings(step).contains(key) { return nil }
        // A PIN in the environment is not the app's to set, whatever the plan read.
        if fix == .pin, context.pin?.place == .environment { return nil }
        let check = step.checks.first { $0.fix == step.fix }
        return fix.action(context, named: check?.title, missing: check?.state == "missing")
    }

    /// The settings the step's page edits in place: the questions the assistant asks itself.
    static func settings(_ step: SetupStep) -> [String] {
        switch step.id {
        case "owner": ["OWNER_NAME", "ALLOWED_CALLERS"]
        case "agents": ["AGENT_BACKEND"]
        default: []
        }
    }

    private static func own(_ step: SetupStep, context: SetupContext) -> SetupActions {
        let done = step.kind == .done
        switch step.id {
        case "migrate":
            return SetupActions(primary: open("Migrate…", .show(.overview)))
        case "voice":
            return voice(context)
        case "agents":
            return agents(context)
        case "owner":
            return SetupActions()
        case "pin":
            guard context.pin?.place != .environment else { return SetupActions() }
            return SetupActions(primary: open(context.pin?.set == true ? "Change PIN…" : "Set PIN…", .setPin))
        case "phone":
            return SetupActions(primary: open("Open Phone", .show(.phone)))
        case "service":
            if done { return SetupActions() }
            let installed = context.service?.units.agent.installed == true
            return SetupActions(primary: SetupAction(title: installed ? "Start Service…" : "Install Service…",
                                                     effect: .installService))
        case "local":
            return local(context)
        case "google":
            return SetupActions(primary: open("Connect Google…", .connect(integration: "google")))
        case "plugins":
            return SetupActions(primary: open("Open Plugins", .show(.plugins)))
        case "issues":
            return SetupActions(primary: open("Connect GitHub…", .connect(integration: "github")))
        default:
            // `profile`, `projects`, and any step this build does not know: the terminal
            // wizard walks them until the app can.
            return SetupActions(primary: SetupAction(title: "Continue in Terminal…", effect: .terminal))
        }
    }

    private static func voice(_ context: SetupContext) -> SetupActions {
        switch context.voice?.kind {
        case .here?:
            return SetupActions(primary: open("Set Up Voice on This Mac…", .setUpVoiceHere),
                                others: [open("Open Voice", .show(.voice))])
        case .server?:
            return SetupActions(primary: open("Open Voice", .show(.voice)))
        default:
            let key = secret("OPENAI_API_KEY", "OpenAI API key", context)
            return SetupActions(primary: key, others: [open("Choose Another Voice", .show(.voice))])
        }
    }

    private static func agents(_ context: SetupContext) -> SetupActions {
        switch context.defaultAgent ?? "claude" {
        case "claude":
            return SetupActions(primary: open("Sign In to Claude…", .signIn(.claude)),
                                others: [secret("ANTHROPIC_API_KEY", "Anthropic API key", context, label: "Use an API Key…")])
        case "codex":
            return SetupActions(primary: open("Sign In to Codex…", .signIn(.codex)),
                                others: [secret("CODEX_API_KEY", "Codex API key", context, label: "Use an API Key…")])
        case "local":
            return SetupActions(primary: open("Open Local Models", .show(.models)))
        default:
            return SetupActions(primary: open("Open Agent Settings", .configure(tab: "agents")))
        }
    }

    /// The local models: choose (and download) one first, then start its server.
    private static func local(_ context: SetupContext) -> SetupActions {
        let models = open("Open Local Models", .show(.models))
        guard let report = context.models, let chosen = report.servers.llm.model,
              report.models.contains(where: { $0.name == chosen && $0.downloaded }) else {
            return SetupActions(primary: open("Choose a Model", .show(.models)))
        }
        guard report.servers.llm.reachable else {
            return SetupActions(primary: SetupAction(title: "Start Model Server", effect: .startModelServer),
                                others: [models])
        }
        if context.modelServerServesAnother != nil {
            return SetupActions(primary: SetupAction(title: "Restart Model Server", effect: .startModelServer),
                                others: [models])
        }
        return SetupActions(primary: models)
    }

    private static func open(_ title: String, _ request: AppRequest) -> SetupAction {
        SetupAction(title: title, effect: .request(request))
    }

    /// "Add OpenAI API Key…", or "Change …" once one is saved; `label` instead when given.
    private static func secret(_ key: String, _ fallback: String, _ context: SetupContext,
                               label: String? = nil) -> SetupAction {
        let row = context.row(key)
        let title = row?.displayTitle ?? fallback
        let replacing = row?.set == true
        let words = label ?? "\(replacing ? "Change" : "Add") \(Wording.titleCase(title))…"
        return SetupAction(title: words, effect: .secret(key: key, title: title, replacing: replacing))
    }
}
