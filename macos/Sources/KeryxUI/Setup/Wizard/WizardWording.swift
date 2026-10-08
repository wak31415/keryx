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

    /// Fresh, resuming, or reviewing; `frames` is how many this run will show.
    package static func welcome(mode: SetupWizardMode, resuming: Bool, left: Int, frames: Int,
                                assistant: String) -> String {
        if mode == .review { return "Go through every setting again. What's set already is filled in." }
        if resuming {
            return left == 1 ? "Pick up where you left off. One thing is still to set."
                : "Pick up where you left off. \(WizardNumbers.word(left)) things are still to set."
        }
        let steps = WizardNumbers.word(max(frames, 2))
        return "\(assistant) answers your calls, talks things through, and hands the work to a coding agent "
            + "on this Mac. \(steps) short steps and you're set."
    }

    package static func welcomeButton(resuming: Bool) -> String { resuming ? "Continue Setup" : "Get Started" }

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
            lines.append("The background service stops while they move, and starts again after.")
        } else if !plan.service.retires.isEmpty || plan.service.installs != nil {
            lines.append("Keryx's background service is put in Jarvis's place.")
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

    package static func aboutTitle(_ assistant: String) -> String { "Who \(assistant) Works For" }
    package static func aboutSentence(_ assistant: String) -> String {
        "What should \(assistant) call you, and which numbers do you call from? \(assistant) answers only these."
    }
    package static let ownerPlaceholder = "the owner"
    package static func ringsThisOne(_ assistant: String) -> String { "\(assistant) rings this one" }

    // MARK: - Models

    package static let modelsTitle = "Models"
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
        case .openai: "The most natural voice, lowest latency; about 6 to 11 cents a minute"
        case .thisMac: tight ? "Tight on this Mac: three open models, slower to answer"
            : "Three open models: one hears, one thinks, one speaks. Free and private; slower"
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
        case .thisMac: "A model on this Mac; free, wants 32 GB or more"
        case .elsewhere: "Ollama, llama.cpp, vLLM or LM Studio"
        }
    }

    package static let speechToSpeech = "Speech to speech: one model listens and answers. The only kind that can call tools today."
    package static let threeModels = "Hear, think, speak: three models, so a small fast one can do the talking."
    package static func sharedWords(tight: Bool, assistant: String) -> String {
        tight ? "Not enough memory for two; \(assistant) will be slower to answer."
            : "\(assistant)'s words come from the work's model."
    }
    package static let sameAsWork = "Same as the work model"

    /// "Apple silicon, 36 GB": this Mac, as the models see it.
    package static func machine(_ hardware: Hardware) -> String {
        let chip = hardware.gpu.flatMap { $0.isEmpty ? nil : $0 } ?? "This Mac"
        return "\(chip), \(Int(hardware.ramGb.rounded())) GB"
    }

    /// The download a choice starts, by its size from the catalog.
    package static func download(_ model: CatalogModel) -> String {
        model.downloaded ? "\(model.title) is downloaded."
            : "\(model.title) is \(Formatting.gigabytes(model.sizeGb)) to download."
    }

    // MARK: - PIN

    package static let pinTitle = "Your PIN"
    package static func pinSentence(_ assistant: String) -> String {
        "Choose a PIN. You key it in on a call before \(assistant) acts for you."
    }
    package static let pinCaption = "Until a PIN exists nothing of yours is read out on the phone."
    package static let pinLater = "Decide on the First Call"

    // MARK: - Phone Line

    package static let phoneTitle = "Phone Line"
    package static func phoneSentence(_ assistant: String) -> String {
        "A number to call \(assistant) on, and a way for Twilio to reach this Mac."
    }
    package static let setUpLater = "Set Up Later"
    package static let pointLater = "Point your number at this Mac from the Phone section."

    // MARK: - Checking and Ready

    package static let checkingTitle = "Checking"
    package static let checkingSentence = "Making sure everything works."
    package static let readyTitle = "You're Ready"
    package static let nearlyTitle = "Nearly Ready"

    package static func ready(_ assistant: String, number: String?, pinSet: Bool) -> String {
        let ring = number.map { "Ring \($0) from your phone." } ?? "Ring your Twilio number from your phone."
        let then = pinSet ? "\(assistant) will say hello and ask a little about you."
            : "\(assistant) will say hello, ask a little about you, and ask you to choose a PIN."
        return "\(assistant) can take your first call. \(ring) \(then)"
    }

    package static func nearlyReady(serviceWaits: Bool) -> String {
        serviceWaits
            ? "Everything but the phone is set up. Connect Twilio from the Phone section when you have an account, "
                + "and Keryx starts running in the background."
            : "Everything but the phone is set up. Connect Twilio from the Phone section when you have an account."
    }

    package static func checkRow(_ row: CheckRow) -> String {
        switch row {
        case .voice: "Voice"
        case .work: "Work"
        case .phone: "Phone"
        case .service: "Background service"
        }
    }

    package static let serviceSentence = "Keryx runs in the background, starts at login and restarts if it stops."
    package static let whenever = "Whenever You Like"
}
