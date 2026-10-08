import Foundation
import KeryxKit

/// What the wizard says. The assistant is named as `ASSISTANT_NAME` names it ("Lyra"), the
/// service is Keryx, and this Mac is "this Mac". One sentence under every title.
package enum WizardWording {
    package static func stepOf(_ index: Int, _ count: Int) -> String { "Step \(index) of \(count)" }

    /// The assistant's name: the setting, else what the status says, else a phrase.
    package static func assistant(_ facts: WizardFacts, status: StatusReport? = nil) -> String {
        facts.text("ASSISTANT_NAME") ?? Wording.assistant(status) ?? "the assistant"
    }

    // MARK: - Welcome

    package static let welcomeTitle = "Welcome to Keryx"

    /// Fresh, resuming, or reviewing; `frames` is how many questions this run asks (its
    /// frames but Welcome and the last).
    package static func welcome(mode: SetupWizardMode, resuming: Bool, left: Int, frames: Int,
                                assistant: String) -> String {
        if mode == .review { return "Go through every setting again. What's set already is filled in." }
        if resuming {
            return left == 1 ? "Pick up where you left off. One thing is still to set."
                : "Pick up where you left off. \(WizardNumbers.word(left)) things are still to set."
        }
        let steps = WizardNumbers.word(max(frames, 1))
        let short = frames == 1 ? "short step" : "short steps"
        return "\(assistant) answers your calls, talks things through, and hands the work to a coding agent "
            + "on this Mac. \(steps) \(short) and you're set."
    }

    package static func welcomeButton(resuming: Bool) -> String { resuming ? "Continue Setup" : "Get Started" }

    /// What a new owner will need, said before they start.
    package static let welcomeNeeds = "You'll need an OpenAI key, a Claude or ChatGPT plan, and a Twilio account "
        + "with a number. The phone can wait."

    package static let needsUpdatingTitle = "Keryx Needs Updating"
    package static let needsUpdating = "The Keryx on this Mac is older than this app and has no setup wizard. "
        + "Update Keryx, then open setup again."
    package static let notFoundTitle = "Keryx Wasn't Found"
    package static let notFound = "This app drives a copy of Keryx installed on this Mac, and couldn't find one. "
        + "Point it at the keryx command, or install Keryx first."
    package static let checkingThisMac = "Checking this Mac…"

    // MARK: - Move Your Files

    package static let moveTitle = "Move Your Files"
    package static let moveSentence = "Keryx used to be called Jarvis, and its files are still where Jarvis kept "
        + "them. Keryx moves them before anything else is set up."

    /// The plan in three lines at most: what moves, the service, the old folder.
    package static func moveSummary(_ plan: MigrationPlan) -> [String] {
        var lines: [String] = []
        let moving = plan.moving.count
        let imports = plan.imports.count
        if moving > 0 {
            lines.append(moving == 1 ? "One file or folder moves to Keryx's folders."
                : "\(moving) files and folders move to Keryx's folders.")
        }
        if imports > 0 {
            lines.append(imports == 1 ? "The settings in an old .env file are imported."
                : "The settings in \(imports) old .env files are imported.")
        }
        if plan.service.stops != nil {
            lines.append("Keryx stops while they move, and starts again after.")
        } else if !plan.service.retires.isEmpty || plan.service.installs != nil {
            lines.append("Keryx answers calls in Jarvis's place.")
        }
        if plan.retire != nil {
            lines.append("The Jarvis folder is renamed aside. Nothing is deleted.")
        }
        if let pending = plan.unitsPending { lines.append(pending) }
        if moving == 0, imports == 0, lines.isEmpty {
            lines.append("Nothing is left to move.")
        }
        return Array(lines.prefix(4))
    }

    /// Why Move Files cannot run now, when it is not conflicts: a call going on.
    package static func moveBlocked(_ plan: MigrationPlan) -> String? {
        guard plan.conflicts.isEmpty, !plan.ready, !plan.nothingToDo else { return nil }
        if let calls = plan.liveCalls, calls > 0 {
            return "A call is going on, and moving the files would cut it off. Try again once the line is clear."
        }
        return nil
    }

    // MARK: - About You

    package static let aboutTitle = "About You"
    package static let aboutSentence = "Your name, the numbers you call from, and the PIN that proves it's you."
    /// Under Your Phone.
    package static func callersCaption(_ assistant: String) -> String { "\(assistant) answers only these." }
    package static let ownerPlaceholder = "the owner"
    package static func ringsThisOne(_ assistant: String) -> String { "\(assistant) rings this one" }

    // MARK: - Models

    package static let modelsTitle = "Voice and Work"
    package static func modelsSentence(_ assistant: String) -> String {
        "Where \(assistant)'s voice runs, and who does the work. Either can be in the cloud or on this Mac."
    }

    package static func voicePlace(_ place: VoicePlace) -> String {
        switch place {
        case .openai: "OpenAI"
        case .thisMac: "This Mac"
        case .elsewhere: "Elsewhere"
        }
    }

    package static func voiceCaption(_ place: VoicePlace, tight: Bool) -> String {
        switch place {
        case .openai: "The most natural voice, quickest to answer — about 6 to 11 cents a minute"
        case .thisMac: tight ? "Tight on this Mac — three open models, slower to answer"
            : "Three open models that hear, think and speak — free and private, slower"
        case .elsewhere: "A voice server of your own"
        }
    }

    package static func workPlace(_ place: WorkPlace) -> String {
        switch place {
        case .claude: "Claude"
        case .codex: "Codex"
        case .thisMac: "This Mac"
        case .elsewhere: "Elsewhere"
        }
    }

    package static func workCaption(_ place: WorkPlace) -> String {
        switch place {
        case .claude: "On your Claude plan, or an API key"
        case .codex: "On your ChatGPT plan, or an API key"
        case .thisMac: "A model on this Mac — free, wants 32 GB or more"
        case .elsewhere: "Ollama, llama.cpp, vLLM or LM Studio"
        }
    }

    package static let speechToSpeech = "One model listens and answers in its own voice."
    package static let threeModels = "Hear, think, speak: three models, so a small fast one can do the talking."
    package static func sharedWords(tight: Bool, assistant: String) -> String {
        tight ? "Not enough memory for two; \(assistant) will be slower to answer."
            : "\(assistant) thinks with the work model, so answers come a little slower."
    }
    package static let sameAsWork = "The work model"
    /// "Thinks" while the voice's words come from elsewhere, chosen in Voice.
    package static let asSetNow = "As Set in Voice"
    package static let loading = "Loading…"
    package static let chooseModel = "Choose a Model"
    package static let keptWords = "Its words stay as they are set in Voice."

    /// "Apple silicon, 36 GB": this Mac, as the models see it.
    package static func machine(_ hardware: Hardware) -> String {
        let chip = hardware.gpu.flatMap { $0.isEmpty ? nil : $0 } ?? "This Mac"
        return "\(chip), \(Int(hardware.ramGb.rounded())) GB"
    }

    /// Where an API key is made, by the setting it goes in.
    package static func keyPage(_ key: String) -> URL? {
        switch key {
        case "OPENAI_API_KEY", "CODEX_API_KEY": URL(string: "https://platform.openai.com/api-keys")
        case "ANTHROPIC_API_KEY": URL(string: "https://console.anthropic.com/settings/keys")
        default: nil
        }
    }

    /// How a cloud agent is signed in, from the doctor's word for its credential.
    package static func signedIn(_ how: String?, agent: String) -> String {
        let said = how?.lowercased() ?? ""
        if said.contains("key") { return "Signed in with an API key" }
        if said.contains("subscription") || said.contains("login") || said.contains("plan") || said.contains("oauth") {
            return agent == "Codex" ? "Signed in on your ChatGPT plan" : "Signed in on your \(agent) plan"
        }
        return "Signed in to \(agent)"
    }

    /// The download a choice starts, by its size from the catalog.
    package static func download(_ model: CatalogModel) -> String {
        model.downloaded ? "\(model.title) is downloaded."
            : "\(model.title) is \(Formatting.gigabytes(model.sizeGb)) to download."
    }

    // MARK: - PIN

    package static func pinCaption(_ assistant: String) -> String {
        "6 to 8 digits, keyed in on a call before \(assistant) acts for you. Until a PIN exists, \(assistant) "
            + "reads nothing of yours out on the phone."
    }
    package static let pinNow = "Choose It Now"
    package static let pinLater = "Set It on My First Call"
    package static func pinLaterCaption(_ assistant: String) -> String {
        "\(assistant) asks you to choose one on your first call."
    }
    /// A PIN Keryx was started with (`KERYX_PIN`): what the wizard says of it, with no word
    /// of an environment or a terminal.
    package static let pinStartedWithTitle = "Set Before Keryx Started"
    package static let pinStartedWith = "Keryx was started with a PIN already chosen, and that one wins. "
        + "To choose a different PIN here, start Keryx without it."
    package static let pinStartedWithBadTitle = "Keryx's PIN Doesn't Work"
    package static let pinStartedWithBad = "Keryx was started with a PIN that isn't 6 to 8 digits, so the phone "
        + "can't use it. Ask whoever set Keryx up to remove it; then a PIN can be chosen here."

    // MARK: - Phone Line

    package static let phoneTitle = "Phone Line"
    package static func phoneSentence(_ assistant: String) -> String {
        "A number to call \(assistant) on, and a way for Twilio to reach this Mac."
    }
    package static let setUpLater = "Set Up Later"
    package static let pointLater = "Point your number at this Mac from the Phone section."
    /// Beside Point It Here….
    package static let pointHereCaption = "Twilio still sends calls to the old address. This is the one change "
        + "Keryx makes outside this Mac."
    /// Sub-step 1, before an account is connected.
    package static let twilioIntro = "Your Account SID and Auth Token are on the front page of the Twilio "
        + "Console. No account yet? A trial one is free and comes with a number."
    /// Sub-step 3: why, before anything is asked.
    package static let reachIntro = "Twilio has to be able to reach this Mac from the internet. Keryx uses "
        + "Cloudflare Tunnel, a small free program, to let calls in."
    package static let addressLabel = "Address for calls"
    package static let addressCaption = "A hostname on a domain you own, routed to this tunnel in Cloudflare."
    package static let noDomain = "No domain? Cloudflare Tunnel needs one that you own. Choose Set Up Later, "
        + "and come back from the Phone section when you have one."
    package static let tunnelMissing = "Not installed yet"
    package static let tunnelInstalled = "Cloudflare Tunnel is installed"
    /// Under "Installing Cloudflare Tunnel…": Homebrew's own lines are for Show Details.
    package static let tunnelInstalling = "Homebrew is downloading it. This takes a minute."
    /// Installed where Homebrew puts it, which the PATH Keryx runs with does not reach.
    package static let tunnelOutsidePath = "Installed, in a folder Keryx doesn't look in yet"

    // MARK: - Checking and Ready

    package static let checkingTitle = "Checking"
    package static let checkingSentence = "Making sure everything works."
    package static let readyTitle = "You're Ready"
    package static let nearlyTitle = "Nearly Ready"

    package static func ready(_ assistant: String, number: String?, pinSet: Bool) -> String {
        let ring = number.map { "Ring \(PhoneDisplay.format($0)) from your phone." }
            ?? "Ring your Twilio number from your phone."
        let then = pinSet ? "\(assistant) will say hello and ask a little about you."
            : "\(assistant) will say hello, ask a little about you, and ask you to choose a PIN."
        return "\(assistant) can take your first call. \(ring) \(then)"
    }

    package static func nearlyReady(serviceWaits: Bool) -> String {
        serviceWaits
            ? "Everything but the phone is set up. Connect Twilio from the Phone section when you have an account, "
                + "and Keryx starts answering calls."
            : "Everything but the phone is set up. Connect Twilio from the Phone section when you have an account."
    }

    /// Everything set up, but the number still rings its old address.
    package static func notPointed(number: String?) -> String {
        let then = number.map { ", then ring \(PhoneDisplay.format($0))" } ?? ""
        return "Everything is set up, but your number still rings its old address. Point it at this Mac "
            + "from Phone in the sidebar\(then)."
    }

    /// A row's title: the four by name, any other step by the plan's title.
    package static func checkRow(_ row: CheckRow, plan: SetupPlan?) -> String {
        switch row {
        case .voice: "Voice"
        case .work: "Work"
        case .phone: "Phone"
        case .service: answeringCalls
        case let .step(id):
            switch id {
            case "owner": "Your Details"
            case "pin": "Your PIN"
            case "migrate": "Your Files from Jarvis"
            case "local": "This Mac's Models"
            default: Wording.titleCase(plan?.step(id)?.title ?? id)
            }
        }
    }

    /// A step's `why` in the app's words: each setting's key said by its title.
    package static func why(_ step: SetupStep?, titles: [String: String]) -> String? {
        guard var why = step?.why, !why.isEmpty else { return nil }
        // "Owner name: …": the check's own title before the colon, which the row's says already.
        if let colon = why.range(of: ": "), why[..<colon.lowerBound].count < 32,
           !why[..<colon.lowerBound].contains("/") {
            why = String(why[colon.upperBound...])
        }
        return Wording.sentence(Wording.capitalised(CheckWording.detail(why, titles: titles).text))
    }

    /// The service step and the Overview's group for it: said as what it is for, the unit
    /// itself kept out of sight.
    package static let answeringCalls = "Answering Calls"
    package static func serviceSentence(_ assistant: String) -> String {
        "While Keryx is open, \(assistant) answers your calls."
    }
    package static let whenever = "Whenever You Like"
}

/// A phone number as a person reads it: E.164 grouped where the grouping is certain (+1, +44),
/// else as saved.
package enum PhoneDisplay {
    package static func format(_ number: String) -> String {
        let digits = number.dropFirst()
        guard number.hasPrefix("+"), digits.allSatisfy(\.isNumber) else { return number }
        if digits.hasPrefix("1"), digits.count == 11 {
            let rest = Array(digits.dropFirst())
            return "+1 \(String(rest[0..<3])) \(String(rest[3..<6])) \(String(rest[6...]))"
        }
        if digits.hasPrefix("44"), digits.count == 12 {
            let rest = Array(digits.dropFirst(2))
            return "+44 \(String(rest[0..<4])) \(String(rest[4...]))"
        }
        return number
    }
}
