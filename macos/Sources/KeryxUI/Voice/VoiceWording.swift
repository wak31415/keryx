import Foundation
import KeryxKit

/// How the Voice screen names the ways the assistant can speak, and what each is doing.
@MainActor enum VoiceWording {
    /// "How Lyra Speaks"; "How the Assistant Speaks" before anything has named it.
    static func sectionTitle(_ assistant: String?) -> String {
        guard let assistant, !assistant.isEmpty else { return "How the Assistant Speaks" }
        return "How \(assistant) Speaks"
    }

    static func title(_ mode: VoiceMode) -> String {
        switch mode {
        case .openai: "OpenAI Realtime"
        case .here: "This Mac"
        case .server: "A Server Elsewhere"
        }
    }

    /// A sentence on what the mode is.
    static func detail(_ mode: VoiceMode) -> String {
        switch mode {
        case .openai: "OpenAI hears the call and speaks the replies. Needs an OpenAI API key."
        case .here: "A voice server on this Mac hears and speaks, and a local model finds the words."
        case .server: "A voice server you run on another machine, reached at its address."
        }
    }

    /// The footer under the modes: what is in use, and that a different choice is not yet.
    static func footer(chosen: VoiceMode, inUse: VoiceMode?) -> String {
        guard let inUse else { return detail(chosen) }
        if chosen == inUse { return "In use now. " + detail(chosen) }
        return detail(chosen) + " Nothing changes until you choose \(primary(chosen)): \(title(inUse)) stays in use."
    }

    /// The button that saves a mode.
    static func primary(_ mode: VoiceMode) -> String {
        switch mode {
        case .openai: "Use OpenAI Realtime"
        case .here: "Set Up"
        case .server: "Check and Use"
        }
    }

    /// What the CLI said, as a sentence — but a first word that is a host or a program
    /// ("studio.local:8000 did not answer") keeps its case.
    static func sentence(_ text: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let last = trimmed.last else { return trimmed }
        let capitalised = Wording.capitalised(trimmed)
        return ".!?…".contains(last) ? capitalised : capitalised + "."
    }

    /// A refusal or failure: the app's sentence for `operation`, with what Keryx said —
    /// its settings named by their titles — under Show Details.
    static func errorText(_ error: KeryxError, operation: String, titles: [String: String]) -> StreamFailure {
        let said = error.said(operation)
        return StreamFailure(said.message, detail: said.detail.map { Wording.named($0, titles: titles) })
    }

    /// What the server section does with the key: send the one typed (when it is not
    /// blank), remove the stored one, or keep it. Never both a key and its removal.
    static func serverKey(typed: String?, clearing: Bool) -> ServerKey {
        if let typed, !typed.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return .typed(typed) }
        return clearing ? .clear : .stored
    }

    /// The key row's state, beside its buttons.
    static func serverKeyState(typed: Bool, clearing: Bool, stored: Bool) -> String {
        if typed { return "Sent with Check and Use" }
        if clearing { return "Removed with Check and Use" }
        return stored ? "Uses the stored key" : "Not set"
    }

    /// A server's address as "host:port"; the address itself when it is not a URL.
    static func address(_ url: String) -> String {
        guard let parts = URLComponents(string: url), let host = parts.host else { return url }
        guard let port = parts.port else { return host }
        return "\(host):\(port)"
    }

    /// What the voice server on this Mac is doing, in a few words.
    static func hereState(_ here: HereVoice) -> String {
        here.reachable ? "Running at 127.0.0.1:\(here.port)" : "Not running"
    }

    /// Under the voice server's title: whether it is installed, and whether it starts at
    /// login — the status beside it says only whether it runs.
    static func hereCaption(_ here: HereVoice) -> String {
        guard here.installed else { return "Not installed" }
        return here.unitInstalled ? "Installed · starts at login" : "Installed"
    }

    /// A step's message, as a sentence-cased line that ends in an ellipsis while it runs.
    static func running(_ message: String) -> String {
        let trimmed = message.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: ".…"))
        guard !trimmed.isEmpty else { return VoiceSetup.starting }
        return Wording.capitalised(trimmed) + "…"
    }

    /// The model the words come from, by its catalog title when the catalog knows it.
    static func wordsModel(_ name: String?, models: [CatalogModel]?) -> String {
        guard let name else { return "None yet" }
        return models?.first { $0.name == name }?.title ?? name
    }

    static let needsModelTitle = "Choose a local model first"

    /// Why This Mac needs a local model first, and where to get the one recommended for it.
    static func needsModelDetail(recommended: CatalogModel?) -> String {
        let why = "This Mac's voice puts its words together with a local model."
        guard let recommended else { return why + " Download one in Local Models and choose Use This Model." }
        if recommended.downloaded {
            return why + " \(recommended.title), recommended for this Mac, is downloaded: choose Use This Model."
        }
        return why + " \(recommended.title) is recommended for this Mac: download it, then choose Use This Model."
    }

    /// The button that shows the model to choose.
    static func needsModelButton(recommended: CatalogModel?) -> String {
        recommended == nil ? "Go to Local Models" : "Show Recommended Model"
    }

    /// The words model's server, under its name: whether it answers, and that Set Up starts
    /// it when it doesn't. `nil` when it is not this Mac's to start (Ollama, elsewhere).
    static func wordsServer(_ report: ModelsReport?) -> (running: Bool, text: String)? {
        guard let report, report.servers.llm.model != nil else { return nil }
        return report.modelServerDown
            ? (false, "Model server not running — Set Up starts it")
            : (true, "Model server running")
    }

    /// "Parakeet TDT and Kokoro" — each by its label in its list (`stt`, `tts`), once read —
    /// and whether they are on this Mac yet.
    static func speechModels(_ here: HereVoice, stt: ConfigOptions? = nil, tts: ConfigOptions? = nil)
        -> (names: String, state: String) {
        func label(_ value: String, _ list: ConfigOptions?) -> String {
            list?.options.first { $0.value == value }?.label ?? value
        }
        return ("\(label(here.stt, stt)) and \(label(here.tts, tts))",
                here.modelsReady ? "Downloaded" : "Set Up downloads them")
    }

    static let setUpQuestion = "Set up the voice server on this Mac?"

    /// What Set Up does, before it is confirmed: the model server first when it isn't
    /// answering.
    static func setUpMessage(startsServer: Bool) -> String {
        let voice = "installs a voice server and its speech models, and starts it in the background."
        let after = " Calls use it once Keryx restarts."
        guard startsServer else { return "Keryx \(voice) The first start can take a few minutes." + after }
        return "The model server isn't running, so Keryx starts it first — installing llama.cpp if it isn't "
            + "here — then \(voice) The first time can take a while." + after
    }

    /// The heading over each run's steps when Set Up runs both.
    static let serverSteps = "Model Server"
    static let voiceSteps = "Voice Server"
    static let setupFailed = "Setup didn't finish"
    static let serverDownTitle = "The model server isn't running"
    static let serverDownDetail = "The voice takes its words from it. Start it, and the setup carries on."
    static let ollamaDownTitle = "Ollama isn't serving the model"
    static let ollamaDownDetail = "The voice takes its words from Ollama's copy of the model. Keryx pulls it into "
        + "Ollama (which must be running), and the setup carries on."

    /// The words come from a server Keryx does not start, and it isn't answering.
    static func elsewhereDown(_ address: String) -> String {
        "The voice takes its words from the model server at \(VoiceWording.address(address)). "
            + "Start it there, then try again."
    }

    static let serverStartingElsewhere = "The model server is starting in Local Models"
    static let speechDownloading = "The speech models are downloading in Local Models"
    static let keyFootnote = "Sent to Keryx when you choose Check and Use, and stored privately on this Mac; "
        + "never shown again."
}
