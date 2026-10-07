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
        case .server: "Check & Use"
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

    /// A server's address as "host:port"; the address itself when it is not a URL.
    static func address(_ url: String) -> String {
        guard let parts = URLComponents(string: url), let host = parts.host else { return url }
        guard let port = parts.port else { return host }
        return "\(host):\(port)"
    }

    /// What the voice server on this Mac is doing, in a few words.
    static func hereState(_ here: HereVoice) -> String {
        if here.reachable { return "Running at 127.0.0.1:\(here.port)" }
        return here.installed ? "Installed, not running" : "Not installed"
    }

    /// The setup steps, in order, for "Step 2 of 4".
    static let steps = ["install", "configure", "unit", "start"]

    /// "Step 2 of 4", for a step this build knows.
    static func stepCount(_ step: String) -> String? {
        guard let index = steps.firstIndex(of: step) else { return nil }
        return "Step \(index + 1) of \(steps.count)"
    }

    /// A step's message, as a sentence-cased line that ends in an ellipsis while it runs.
    static func running(_ message: String) -> String {
        let trimmed = message.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: ".…"))
        guard !trimmed.isEmpty else { return VoiceSetup.starting }
        return Wording.capitalised(trimmed) + "…"
    }

    /// "120 MB of 480 MB".
    static func sizes(_ progress: VoiceSetupEvent.Progress) -> String {
        ModelRow.progress(done: progress.done, total: progress.total)
    }

    /// The model the words come from, by its catalog title when the catalog knows it.
    static func wordsModel(_ name: String?, models: [CatalogModel]?) -> String {
        guard let name else { return "None yet" }
        return models?.first { $0.name == name }?.title ?? name
    }

    static let needsModelTitle = "Set up a local model first"
    static let needsModelDetail =
        "This Mac's voice puts its words together with a local model. Download one and choose Use This Model."
    static let setUpQuestion = "Set up the voice server on this Mac?"
    static let setUpMessage = "Keryx installs a voice server and its speech models, and starts it in the background. "
        + "The first start can take a few minutes. Calls use it once Keryx restarts."
    static let keyFootnote = "Sent to Keryx on its standard input when you choose Check & Use, and kept in "
        + "secrets.toml, readable only by you."
}
