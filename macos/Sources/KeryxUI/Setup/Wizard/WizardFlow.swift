import Foundation
import KeryxKit

/// A frame of the setup wizard, in the order it walks them.
package enum WizardFrame: String, CaseIterable, Hashable, Sendable {
    case welcome
    /// Move Your Files: only while Jarvis's files are still to move.
    case migrate
    /// Your name, your phones and your PIN.
    case aboutYou
    case models
    case phone
    /// Checking, which becomes You're Ready (or Nearly Ready) in place.
    case finish
}

/// What the wizard decides its frames from: the plan, and the settings. A frame is a view of
/// these; nothing the wizard knows is kept anywhere else.
package struct WizardFacts: Equatable, Sendable {
    package var plan: SetupPlan?
    package var config: [ConfigRow]?

    package init(plan: SetupPlan?, config: [ConfigRow]? = nil) {
        self.plan = plan
        self.config = config
    }

    @MainActor package init(_ model: AppModel) {
        self.init(plan: model.setupPlan.value, config: model.config.value)
    }

    /// The plan's step `id` is done.
    package func done(_ id: String) -> Bool { plan?.step(id)?.kind == .done }

    package func row(_ key: String) -> ConfigRow? { config?.first { $0.key == key } }

    /// A plain setting's text, `nil` when it is empty or unset.
    package func text(_ key: String) -> String? {
        let text = ConfigWording.editText(row(key)?.value).trimmingCharacters(in: .whitespaces)
        return text.isEmpty ? nil : text
    }

    /// The step `id` is still to do, as the plan has it now.
    package func needs(_ id: String) -> Bool {
        guard let step = plan?.step(id) else { return false }
        return step.kind != .done
    }
}

/// The wizard's sequence, as rules: which frames a run shows, in what order, where it goes
/// on Continue, and how far along the dot strip is. It decides nothing the plan decides.
package enum WizardFlow {
    /// Whether `frame` is shown, now. First-run skips what is done; review shows every frame
    /// but the migration, which is only ever there while files are still to move.
    package static func shows(_ frame: WizardFrame, mode: SetupWizardMode, facts: WizardFacts) -> Bool {
        switch frame {
        case .welcome, .finish:
            return true
        case .migrate:
            return facts.needs("migrate")
        case .aboutYou, .models, .phone:
            guard mode == .firstRun else { return true }
            return !steps(of: frame).allSatisfy(facts.done)
        }
    }

    /// The plan's steps a frame answers: once all of them are done, first-run skips it.
    package static func steps(of frame: WizardFrame) -> [String] {
        switch frame {
        case .welcome, .finish: []
        case .migrate: ["migrate"]
        case .aboutYou: ["owner", "pin"]
        case .models: ["voice", "agents"]
        case .phone: ["phone"]
        }
    }

    /// The frame Continue goes to from `frame`: the next in order that shows.
    package static func next(after frame: WizardFrame, mode: SetupWizardMode, facts: WizardFacts) -> WizardFrame? {
        upcoming(after: frame, mode: mode, facts: facts).first
    }

    /// The frames still to come after `frame`, as things are now.
    package static func upcoming(after frame: WizardFrame, mode: SetupWizardMode, facts: WizardFacts) -> [WizardFrame] {
        let order = WizardFrame.allCases
        guard let index = order.firstIndex(of: frame) else { return [] }
        return order[(index + 1)...].filter { shows($0, mode: mode, facts: facts) }
    }

    /// The questions this run asks: the frames it shows but Welcome and the last, for
    /// Welcome's "Three short steps".
    package static func questions(mode: SetupWizardMode, facts: WizardFacts) -> Int {
        upcoming(after: .welcome, mode: mode, facts: facts).filter { $0 != .finish && $0 != .migrate }.count
    }

    /// Where the dot strip is: the frame shown (1-based) of all this run will show — the
    /// frames already walked, and those still to come.
    package static func position(path: [WizardFrame], mode: SetupWizardMode, facts: WizardFacts)
        -> (index: Int, count: Int) {
        guard let current = path.last else { return (0, 0) }
        return (path.count, path.count + upcoming(after: current, mode: mode, facts: facts).count)
    }

    /// The run picks up where it was left: first-run, not ready, and something required
    /// already done or walked.
    package static func resuming(mode: SetupWizardMode, facts: WizardFacts) -> Bool {
        guard mode == .firstRun, let plan = facts.plan, !plan.ready else { return false }
        return plan.steps.contains { $0.required && $0.id != "migrate" && ($0.kind == .done || $0.walked) }
    }
}

// MARK: - About You

/// The owner's phones, as typed: validated as they are, one per field.
package enum CallerNumbers {
    /// E.164: a plus, then 8 to 15 digits, the first not 0. Spaces, dashes, dots and
    /// brackets typed between the digits are allowed and dropped.
    package static func normalised(_ typed: String) -> String? {
        let kept = typed.filter { !" -.()\u{00A0}".contains($0) }
        guard kept.first == "+" else { return nil }
        let digits = kept.dropFirst()
        guard (8...15).contains(digits.count), digits.allSatisfy({ $0.isASCII && $0.isNumber }),
              digits.first != "0" else {
            return nil
        }
        return kept
    }

    /// What is wrong with one field, said under it; `nil` for a field that is fine or empty.
    package static func problem(_ typed: String) -> String? {
        let trimmed = typed.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty, normalised(trimmed) == nil else { return nil }
        if !trimmed.hasPrefix("+") { return "Start with + and the country code, as in +44 7700 900123." }
        return "That isn't a whole phone number yet."
    }

    /// The numbers, normalised, blanks dropped; `nil` while any field is not a number, or
    /// none is given.
    package static func list(_ fields: [String]) -> [String]? {
        let typed = fields.map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        let numbers = typed.compactMap(normalised)
        guard !numbers.isEmpty, numbers.count == typed.count else { return nil }
        var seen: Set<String> = []
        return numbers.filter { seen.insert($0).inserted }
    }

    /// The saved numbers, the one Keryx rings (`OWNER_NUMBER`) first.
    package static func saved(_ facts: WizardFacts) -> [String] {
        var numbers: [String] = []
        if case let .array(items)? = facts.row("ALLOWED_CALLERS")?.value {
            numbers = items.map(\.displayText).filter { !$0.isEmpty }
        } else if let text = facts.text("ALLOWED_CALLERS") {
            numbers = text.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
        }
        if let owner = facts.text("OWNER_NUMBER"), let index = numbers.firstIndex(of: owner), index > 0 {
            numbers.remove(at: index)
            numbers.insert(owner, at: 0)
        }
        return numbers
    }
}

/// About You's answers, and the settings they change.
package struct AboutYouAnswers: Equatable, Sendable {
    package var name: String
    package var assistant: String
    package var numbers: [String]

    package init(name: String, assistant: String, numbers: [String]) {
        self.name = name
        self.assistant = assistant
        self.numbers = numbers
    }

    /// The coding agents, whose names an assistant may not take.
    static let agentNames: Set<String> = ["claude", "codex"]

    /// Why the assistant's name will not do; `nil` when it will.
    package static func assistantProblem(_ name: String) -> String? {
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        if trimmed.isEmpty { return "Give the assistant a name." }
        if agentNames.contains(trimmed.lowercased()) { return "That's a coding agent's name." }
        return nil
    }

    /// Only what changed, in a fixed order: the name, the assistant's name, the numbers and
    /// the one rung first. A blank name unsets one that was saved ("the owner").
    package func writes(from facts: WizardFacts) -> [WizardWrite] {
        var writes: [WizardWrite] = []
        let name = self.name.trimmingCharacters(in: .whitespaces)
        let savedName = facts.text("OWNER_NAME")
        if name.isEmpty {
            if savedName != nil { writes.append(.unset("OWNER_NAME")) }
        } else if name != savedName {
            writes.append(.set("OWNER_NAME", name))
        }
        let assistant = self.assistant.trimmingCharacters(in: .whitespaces)
        if !assistant.isEmpty, assistant != (facts.text("ASSISTANT_NAME") ?? "") {
            writes.append(.set("ASSISTANT_NAME", assistant))
        }
        if let numbers = CallerNumbers.list(self.numbers) {
            if numbers != CallerNumbers.saved(facts) || facts.row("ALLOWED_CALLERS")?.set != true {
                writes.append(.set("ALLOWED_CALLERS", numbers.joined(separator: ",")))
            }
            if let first = numbers.first, first != facts.text("OWNER_NUMBER") {
                writes.append(.set("OWNER_NUMBER", first))
            }
        }
        return writes
    }
}

// MARK: - The PIN

/// How the owner chose to settle the PIN on About You.
package enum PinChoice: Hashable, Sendable {
    /// Typed twice on the frame.
    case now
    /// Chosen by whoever calls first (`setup mark pin`).
    case firstCall
}

/// What About You's Continue does with the PIN.
package enum PinSave: Equatable, Sendable {
    /// Nothing: a PIN is in place, one the app cannot change, or the section is folded.
    case nothing
    /// `setup mark pin`: whoever calls first chooses it.
    case firstCall
    /// `pin set --stdin`: the digits typed twice.
    case digits
}

/// The PIN's part of About You. A PIN is typed on the frame only where there is none at
/// all; any PIN file — even one holding no usable PIN — is changed through the PIN sheet,
/// whose "Replace the PIN?" is the explicit yes that reopens the enrolment door.
package enum PinRules {
    /// The fields are offered: no PIN anywhere.
    package static func asksForDigits(_ status: PinStatus?) -> Bool {
        guard let status else { return false }
        return PinWording.kind(status) == .none
    }

    /// The section is answered: shown folded (settled already), a PIN in place (or one the
    /// app cannot change), the first call chosen, or the same usable digits typed twice.
    package static func answered(_ status: PinStatus?, choice: PinChoice, first: String, second: String,
                                 folded: Bool = false) -> Bool {
        guard let status else { return false }
        guard asksForDigits(status), !folded else { return true }
        return choice == .firstCall || PinWording.ready(first: first, second: second)
    }

    /// What Continue does. `folded`: the section is shown folded, settled as it is; only
    /// then is a done step left alone — in a review it is open, and digits typed there are
    /// a PIN to set, whatever the plan said. `stepDone`: the plan has the PIN settled, so
    /// leaving it to the first call again writes nothing.
    package static func save(_ status: PinStatus?, choice: PinChoice, folded: Bool, stepDone: Bool) -> PinSave {
        guard asksForDigits(status), !folded else { return .nothing }
        switch choice {
        case .now: return .digits
        case .firstCall: return stepDone ? .nothing : .firstCall
        }
    }
}

/// About You's Continue, in order: the settings, then the PIN.
package enum AboutYouSave {
    package enum Outcome: Equatable {
        case saved
        case failed(KeryxError)
        /// The CLI refused the digits themselves: said under the fields.
        case digits(String)
    }

    @MainActor package static func run(_ writes: [WizardWrite], pin: PinSave, digits: String, status: PinStatus?,
                                       model: AppModel) async -> Outcome {
        var all = writes
        if pin == .firstCall { all.append(.mark("pin")) }
        if let error = await WizardWrite.apply(all, with: model) { return .failed(error) }
        guard pin == .digits, let status else { return .saved }
        switch await model.setPin(digits, replace: PinWording.replaces(status)) {
        case .success: return .saved
        case let .failure(error):
            let said = PinWording.failure(error)
            return said.aboutTheDigits ? .digits(said.text) : .failed(error)
        }
    }
}

// MARK: - Writing

/// One thing a frame writes on Continue, each through `AppModel`'s own write path: a setting
/// set or unset (`config set|unset`), one of a setting's options chosen, the voice put back
/// on OpenAI (`voice use openai`), a step recorded (`setup mark`).
package enum WizardWrite: Equatable, Sendable {
    case set(String, String)
    case unset(String)
    case choose(String, String)
    case voiceOpenAI
    case mark(String)

    /// Runs `writes` in order, stopping at the first that fails, whose error it returns.
    @MainActor package static func apply(_ writes: [WizardWrite], with model: AppModel) async -> KeryxError? {
        for write in writes {
            let error: KeryxError? = switch write {
            case let .set(key, value): await model.set(key: key, to: value)
            case let .unset(key): await model.unset(key)
            case let .choose(key, value): await model.choose(key, option: value)
            case .voiceOpenAI: await model.useVoiceOpenAI()
            case let .mark(step): await model.markSetupStep(step)
            }
            if let error { return error }
        }
        return nil
    }
}

// MARK: - Models

/// Where Lyra's voice runs.
package enum VoicePlace: String, CaseIterable, Identifiable, Sendable {
    case openai, thisMac, elsewhere

    package var id: String { rawValue }

    /// As things are set up now; OpenAI when nothing says otherwise.
    package static func current(_ voice: VoiceStatus?) -> VoicePlace {
        switch voice?.kind {
        case .here?: .thisMac
        case .server?: .elsewhere
        default: .openai
        }
    }
}

/// Who does the work.
package enum WorkPlace: String, CaseIterable, Identifiable, Sendable {
    case claude, codex, thisMac, elsewhere

    package var id: String { rawValue }

    /// The sign-in a cloud agent takes; `nil` for a local model.
    package var provider: SignInProvider? {
        switch self {
        case .claude: .claude
        case .codex: .codex
        case .thisMac, .elsewhere: nil
        }
    }

    /// `AGENT_BACKEND` for it.
    package var backend: String {
        switch self {
        case .claude: "claude"
        case .codex: "codex"
        case .thisMac, .elsewhere: "local"
        }
    }

    /// As things are set up now: the default agent, a local one here or at an address.
    /// Claude when nothing says otherwise.
    package static func current(_ facts: WizardFacts, models: ModelsReport?) -> WorkPlace {
        switch facts.text("AGENT_BACKEND") {
        case "codex"?: return .codex
        case "local"?:
            if case .elsewhere? = models?.wordsServer { return .elsewhere }
            return .thisMac
        default: return .claude
        }
    }
}

/// What was chosen on Models: where each runs, and what was picked for it (`nil`: what is
/// set already).
package struct ModelsChoice: Equatable, Sendable {
    package var voice: VoicePlace
    package var work: WorkPlace
    package var hears: String?
    package var speaks: String?
    package var speaksIn: String?
    /// The voice's own model (SEAM), "" for the work's.
    package var wordsModel: String?
    package var serverModel: String?
    package var harness: String?

    package init(voice: VoicePlace, work: WorkPlace, hears: String? = nil, speaks: String? = nil,
                 speaksIn: String? = nil, wordsModel: String? = nil, serverModel: String? = nil,
                 harness: String? = nil) {
        self.voice = voice
        self.work = work
        self.hears = hears
        self.speaks = speaks
        self.speaksIn = speaksIn
        self.wordsModel = wordsModel
        self.serverModel = serverModel
        self.harness = harness
    }
}

/// The settings' keys that were renamed: the name the CLI lists, so the wizard writes the
/// one this Keryx knows.
package enum WizardKeys {
    /// `AGENT_SERVER_MODEL`, once `LLM_SERVER_MODEL` was renamed to it.
    package static func agentServerModel(_ facts: WizardFacts) -> String {
        facts.row("AGENT_SERVER_MODEL") != nil ? "AGENT_SERVER_MODEL" : "LLM_SERVER_MODEL"
    }
}

/// The rules of the Models frame: what each section's defaults are, and when it is answered.
package enum ModelsRules {
    /// Less memory than this for models, and the voice on this Mac is tight.
    package static let tightBelowGB = 24.0

    package static func tight(_ models: ModelsReport?) -> Bool {
        guard let memory = models?.hardware.modelMemoryGb else { return false }
        return memory < tightBelowGB
    }

    /// The model the work defaults to on this Mac: the CLI's recommendation when it says
    /// (SEAM: `recommended`, or `recommended_with_local_voice` beside a local voice), else
    /// the catalog's recommended one that fits; the one chosen already wins.
    package static func workModel(_ models: ModelsReport?, voiceHere: Bool) -> String? {
        guard let models else { return nil }
        if let chosen = models.servers.llm.model, models.models.contains(where: { $0.name == chosen }) {
            return chosen
        }
        let said = voiceHere ? (models.recommendedWithLocalVoice ?? models.recommended) : models.recommended
        if let said { return said }
        return models.models.first { $0.recommended && $0.fits }?.name ?? models.models.first(where: \.fits)?.name
    }

    /// The catalog models that fit, for a pop-up: the recommended first is not reordered —
    /// the catalog's order is the CLI's.
    package static func fitting(_ models: ModelsReport?) -> [CatalogModel] {
        models?.models.filter(\.fits) ?? []
    }

    /// The voice's "Thinks" model when it is a control of its own (SEAM: the CLI says how
    /// it fits beside the agent's, `voice_llm_separate`): separate, the CLI's recommendation;
    /// shared, `nil` — the work's model.
    package static func voiceWordsModel(_ models: ModelsReport?) -> String? {
        guard models?.voiceLlmSeparate == true else { return nil }
        return models?.voiceLlmRecommended
    }

    /// The voice's own model is a control: the CLI says what the owner's rule makes of this
    /// Mac (SEAM: `voice_llm_separate`) and has a setting for it (`VOICE_LLM_MODEL`).
    package static func voiceWordsIsAControl(_ models: ModelsReport?, facts: WizardFacts) -> Bool {
        models?.voiceLlmSeparate != nil && facts.row("VOICE_LLM_MODEL") != nil
    }

    /// The voice's section is answered: a key that is saved, a voice server that was saved,
    /// or this Mac's models chosen — and, with the work in the cloud and no voice model of
    /// its own, a model for its words.
    package static func voiceAnswered(_ place: VoicePlace, voice: VoiceStatus?, keySaved: Bool,
                                      wordsModel: String?) -> Bool {
        switch place {
        case .openai: keySaved
        case .thisMac: wordsModel != nil
        case .elsewhere: voice?.kind == .server
        }
    }

    /// The work's section is answered: the agent signed in (or this run's sign-in finished,
    /// or its API key saved), or a local model chosen, or a server and its model saved.
    package static func workAnswered(_ place: WorkPlace, signedIn: Bool, keySaved: Bool, model: String?,
                                     facts: WizardFacts) -> Bool {
        switch place {
        case .claude, .codex: signedIn || keySaved
        case .thisMac: model != nil
        case .elsewhere: facts.text("LOCAL_AGENT_BASE_URL") != nil && facts.text("LOCAL_AGENT_MODEL") != nil
        }
    }

    /// What Models' Continue writes for the choices made: the voice put on OpenAI when it is
    /// not; this Mac's speech models as picked, and (SEAM) the voice's own model; the default
    /// agent (one pick, so `AGENTS_ENABLED` is emptied with it); a model server elsewhere's
    /// model and harness, the local agent made the default, and this Mac's model server
    /// cleared. A local model here is chosen once it is downloaded (`LocalWork`), not here.
    package static func writes(_ choice: ModelsChoice, facts: WizardFacts, voice: VoiceStatus?,
                               models: ModelsReport?) -> [WizardWrite] {
        var writes: [WizardWrite] = []
        switch choice.voice {
        case .openai:
            if voice?.kind != .openai { writes.append(.voiceOpenAI) }
        case .thisMac:
            let here = voice?.here
            for (key, picked, saved) in [("VOICE_SERVER_STT", choice.hears, here?.stt),
                                         ("VOICE_SERVER_TTS", choice.speaks, here?.tts),
                                         ("VOICE_SERVER_VOICE", choice.speaksIn, here?.voice)] {
                if let picked, picked != saved { writes.append(.choose(key, picked)) }
            }
            if voiceWordsIsAControl(models, facts: facts) {
                let own = choice.wordsModel ?? ""
                if facts.row("VOICE_LLM") != nil { writes.append(.set("VOICE_LLM", own.isEmpty ? "shared" : "here")) }
                if !own.isEmpty, own != facts.text("VOICE_LLM_MODEL") { writes.append(.set("VOICE_LLM_MODEL", own)) }
            }
        case .elsewhere:
            break
        }
        switch choice.work {
        case .claude, .codex:
            if facts.text("AGENT_BACKEND") != choice.work.backend {
                writes += [.set("AGENT_BACKEND", choice.work.backend), .set("AGENTS_ENABLED", "")]
            }
        case .thisMac:
            break
        case .elsewhere:
            for (key, picked) in [("LOCAL_AGENT_MODEL", choice.serverModel), ("LOCAL_AGENT_API", choice.harness)] {
                if let picked, !picked.isEmpty, picked != facts.text(key) { writes.append(.set(key, picked)) }
            }
            if facts.text("AGENT_BACKEND") != "local" {
                writes += [.set("AGENT_BACKEND", "local"), .set("AGENTS_ENABLED", "")]
            }
            let server = WizardKeys.agentServerModel(facts)
            if facts.row(server)?.set == true { writes.append(.unset(server)) }
        }
        return writes
    }

    /// The setting a cloud agent's API key is kept in.
    package static func keyFor(_ place: WorkPlace) -> String? {
        switch place {
        case .claude: "ANTHROPIC_API_KEY"
        case .codex: "CODEX_API_KEY"
        case .thisMac, .elsewhere: nil
        }
    }
}

// MARK: - Checking

/// One row of the Checking frame: the four a first call needs, and any other step the plan
/// still holds open (the owner, the PIN, a migration, the local models), so nothing it
/// waits for is ever off the list.
package enum CheckRow: Hashable, Identifiable, Sendable {
    case voice, work, phone, service
    /// Another step of the plan, by its id.
    case step(String)

    package static let fixed: [CheckRow] = [.voice, .work, .phone, .service]

    package var id: String { step }

    /// The plan's step it reads.
    package var step: String {
        switch self {
        case .voice: "voice"
        case .work: "agents"
        case .phone: "phone"
        case .service: "service"
        case let .step(id): id
        }
    }

    /// The frame that sets it, for Fix…; `nil` for a step no frame sets (the main window's
    /// checklist mends it).
    package var frame: WizardFrame? {
        switch step {
        case "voice", "agents", "local": .models
        case "phone": .phone
        case "owner", "pin": .aboutYou
        case "migrate": .migrate
        default: nil
        }
    }

    /// The rows for `plan`: the four, then every other required step not done, then the
    /// local models when this run puts something on this Mac and they are not done.
    package static func rows(_ plan: SetupPlan?, local: Bool) -> [CheckRow] {
        let known = Set(fixed.map(\.step))
        var extra = (plan?.remaining ?? []).map(\.id).filter { !known.contains($0) }
        if local, let step = plan?.step("local"), step.kind != .done, !extra.contains("local") { extra.append("local") }
        return fixed + extra.map(CheckRow.step)
    }
}

/// How the Checking frame ends.
package enum WizardEnding: Equatable, Sendable {
    /// Something required is still being checked or set up.
    case checking
    /// Every required step is done, and the number rings this Mac.
    case ready
    /// Everything is, but the phone, left for later — and what waits for it.
    case nearlyReady
    /// Everything is set up, but the number still rings its old address: no call arrives.
    case notPointed
}

package enum CheckingRules {
    /// The ending, from the plan: ready when it is — unless Twilio says the number rings
    /// elsewhere (`pointed` false); nearly ready when all that is left was left for later —
    /// as the plan says (SEAM: `deferred`), or, from a CLI that cannot defer, when the phone
    /// was left for later in this run and nothing else required but what waits on the phone
    /// is left.
    package static func ending(_ plan: SetupPlan?, phoneLater: Bool, pointed: Bool? = nil) -> WizardEnding {
        guard let plan else { return .checking }
        if plan.ready { return pointed == false ? .notPointed : .ready }
        if plan.speaksOfDeferring { return plan.nearlyReady ? .nearlyReady : .checking }
        guard phoneLater else { return .checking }
        let left = plan.remaining.filter { $0.id != "phone" }
        // What is left waits for the phone alone.
        let waitsForThePhone = { (step: SetupStep) -> Bool in
            step.kind == .blocked && step.blockedBy.allSatisfy { $0 == "phone" || plan.step($0)?.kind == .done }
        }
        return left.allSatisfy(waitsForThePhone) ? .nearlyReady : .checking
    }

    /// The step was left for later: the plan says so (SEAM: `deferred`), or — from a CLI
    /// that cannot defer — it is the phone and this run left it.
    package static func leftForLater(_ step: SetupStep?, plan: SetupPlan?, phoneLater: Bool) -> Bool {
        guard let step, step.kind != .done else { return false }
        if plan?.speaksOfDeferring == true { return step.deferred == true }
        return phoneLater && step.id == "phone"
    }

    /// The background service may be installed now: its step is needed (or failed), and
    /// waits for nothing.
    package static func serviceInstallable(_ plan: SetupPlan?) -> Bool {
        guard let step = plan?.step("service") else { return false }
        return step.kind == .needed || step.kind == .failed
    }

    /// Reaching it records setup as seen through: ready, nearly ready, or set up but for
    /// pointing the number. Still checking never does, however the wizard is left.
    package static func completes(_ ending: WizardEnding) -> Bool {
        ending != .checking
    }

    /// The step a row shows as stopped: the row's next step, when the owner stopped it.
    /// One stopped for an earlier choice, or a step since done, shows nothing.
    package static func stopped(_ row: CheckRow, next: LocalStep, intent: LocalIntent,
                                stopped: Set<LocalStep>) -> LocalStep? {
        guard stopped.contains(next), next.row(intent) == row else { return nil }
        return next
    }

    /// Checking starts this Mac's work and the service by itself only in a first run, and
    /// never what the owner stopped; a review starts nothing without a click.
    package static func startsByItself(_ step: LocalStep, mode: SetupWizardMode, approved: Bool,
                                       stopped: Set<LocalStep>) -> Bool {
        guard !stopped.contains(step) else { return false }
        return mode == .firstRun || approved
    }
}

// MARK: - Words

package enum WizardNumbers {
    /// "Six" for 6: the frames this run shows, said in a sentence.
    package static func word(_ count: Int) -> String {
        let words = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"]
        return words.indices.contains(count) ? words[count] : String(count)
    }
}
