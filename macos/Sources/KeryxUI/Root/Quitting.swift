import AppKit
import KeryxKit

/// What quitting asks while something is still running that quitting would stop.
public enum Quitting {
    public struct Question: Equatable, Sendable {
        public let message: String
        public let detail: String
        /// Whether it may carry "Don't ask again": only the plain "Quit Keryx?", never one
        /// that names something under way or a call in progress.
        public var suppressible = false
    }

    /// Quitting while Jarvis's files move: refused, said why.
    public static let migratingTitle = "Keryx Is Moving Its Files"
    public static let migratingDetail = "Quit once the move has finished. Stopped part way, it would leave "
        + "Keryx stopped and its files half moved."

    /// The question for what is under way — a model download, a voice setup, a model
    /// server start, a speech model download, the voice LLM changed or its server started,
    /// a sign-in, Keryx starting or stopping, any of
    /// them together; `nil` when none is and the app can just quit.
    /// `signingIn`: what a sign-in under way signs in to ("Claude"), and `signInDetail` what
    /// stopping it does. `assistant`: the assistant's name ("Lyra"). `writing`: the short
    /// writes still going after the moment quitting gave them (`AppModel.writing`).
    ///
    /// `stopsAnswering`: quitting stops Keryx answering calls (`AppModel.quitStopsAnswering`),
    /// which every question then says; with nothing else under way it is asked only while
    /// `liveCalls` are in progress, which quitting ends, or when `confirm` (the owner has not
    /// ticked "Don't ask again").
    public static func question(downloading: Bool, settingUpVoice: Bool, startingModelServer: Bool = false,
                                downloadingVoiceModels: Bool = false, changingVoiceLLM: Bool = false,
                                startingVoiceLLMServer: Bool = false,
                                signingIn: String? = nil,
                                signInDetail: String? = nil, assistant: String?,
                                changingService: AppModel.ServiceChange? = nil,
                                writing: [AppModel.Write] = [], stopsAnswering: Bool = false,
                                liveCalls: Int = 0, confirm: Bool = false) -> Question? {
        // What is under way, as a clause; what quitting does to it alone; and how it comes
        // back, said beside the others.
        var under: [(clause: String, alone: String, again: String)] = []
        if downloading {
            under.append(("a model is downloading", "It will pick up where it left off next time.",
                          "the download picks up where it left off"))
        }
        if settingUpVoice {
            under.append(("\(possessive(assistant, opening: false)) voice is being set up",
                          "The setup stops. Set Up… in Voice starts it again.",
                          "Set Up… in Voice starts the setup again"))
        }
        if startingModelServer {
            under.append(("the model server is starting",
                          "It stops, and won't start again by itself. Start Model Server in Local Models starts it again.",
                          "Start Model Server in Local Models starts the model server again"))
        }
        if downloadingVoiceModels {
            under.append(("the speech models are downloading",
                          "Download in Local Models picks up where it left off.",
                          "the speech models pick up where they left off"))
        }
        if changingVoiceLLM {
            // Stopped part way, the CLI puts back the words the voice had; quitting waits for it.
            under.append(("\(possessive(assistant, opening: false)) voice LLM is being changed",
                          "It stops, and the voice is put back on the words it had. Voice changes it again.",
                          "Voice changes the voice LLM again"))
        }
        if startingVoiceLLMServer {
            under.append(("the voice LLM's server is starting",
                          "It stops, and won't start again by itself. Start Voice LLM Server in Local Models starts it again.",
                          "Start Voice LLM Server in Local Models starts the voice LLM's server again"))
        }
        if let signingIn {
            under.append(("a sign-in to \(signingIn) is still going",
                          signInDetail ?? "The sign-in stops. You can start it again later.",
                          "the sign-in to \(signingIn) can be started again later"))
        }
        if let changingService {
            // Quitting stops the installer (its whole process group); the next launch starts
            // Keryx again, and quitting stops it whatever state the start was left in.
            switch changingService {
            case .installing:
                under.append(("Keryx is starting", "It stops part way.", "Keryx stops starting"))
            case .uninstalling:
                under.append(("Keryx is stopping", "Quitting finishes stopping it.", "Keryx finishes stopping"))
            }
        }
        if !writing.isEmpty {
            // Cut short, a write can leave its change half made (one webhook of two).
            let alone = writing.count == 1
                ? "It stops part way, and may leave the change half made."
                : "They stop part way, and may leave their changes half made."
            under.append((list(writing.map(\.clause), comma: false), alone,
                          "a change cut short may be left half made"))
        }
        // What quitting does to calls, said after whatever else is under way.
        let calls = stopsAnswering ? answering(assistant, liveCalls: liveCalls) : nil
        let detail = { (text: String) in [text, calls].compactMap { $0 }.joined(separator: " ") }
        guard let first = under.first else {
            guard stopsAnswering, let calls else { return nil }
            if liveCalls > 0 {
                let message = liveCalls == 1 ? "1 call is in progress." : "\(liveCalls) calls are in progress."
                return Question(message: message + " Quit anyway?", detail: calls)
            }
            guard confirm else { return nil }
            return Question(message: "Quit Keryx?", detail: calls + " " + closeInstead, suppressible: true)
        }
        guard under.count > 1 else {
            return Question(message: sentence(first.clause) + ". Quit anyway?", detail: detail(first.alone))
        }
        let doing = list(under.map(\.clause), comma: false)
        let again = list(under.map(\.again), comma: true)
        return Question(message: sentence(doing) + ". Quit anyway?",
                        detail: detail((under.count == 2 ? "Both stop. " : "They all stop. ") + sentence(again) + "."))
    }

    /// "Lyra stops answering calls until you open Keryx again.", with the calls in progress
    /// it ends first.
    public static func answering(_ assistant: String?, liveCalls: Int = 0) -> String {
        let name = (assistant?.isEmpty == false ? assistant : nil) ?? "The assistant"
        let ends = switch liveCalls {
        case 0: ""
        case 1: "The call in progress ends, and "
        default: "The \(liveCalls) calls in progress end, and "
        }
        let who = ends.isEmpty ? name : (assistant?.isEmpty == false ? name : "the assistant")
        return ends + "\(who) stops answering calls until you open Keryx again."
    }

    /// What the plain question adds: closing the window is how to keep answering.
    public static let closeInstead = "To keep answering, close the window instead."

    /// "a and b", "a, b and c"; with `comma`, a comma before the "and" too, for clauses
    /// long enough to want one.
    private static func list(_ parts: [String], comma: Bool) -> String {
        guard let last = parts.last, parts.count > 1 else { return parts.first ?? "" }
        return parts.dropLast().joined(separator: ", ") + (comma ? ", and " : " and ") + last
    }

    /// The first letter raised.
    private static func sentence(_ text: String) -> String {
        text.prefix(1).uppercased() + text.dropFirst()
    }

    /// "Lyra's", or "the assistant's" before a name is known ("The" opening a sentence).
    private static func possessive(_ name: String?, opening: Bool) -> String {
        guard let name, !name.isEmpty else { return opening ? "The assistant's" : "the assistant's" }
        return name + "'s"
    }
}

/// Which menus opening should refresh what the menu bar lists.
@MainActor public enum MenuTracking {
    /// `menu` is the menu of a pop-up button (a `Picker` in one of the windows): opening it
    /// is choosing a value, not looking at the menu bar.
    public static func isPopUpMenu(_ menu: NSMenu, in windows: [NSWindow]) -> Bool {
        windows.contains { window in
            guard let view = window.contentView else { return false }
            return contains(view, menu)
        }
    }

    private static func contains(_ view: NSView, _ menu: NSMenu) -> Bool {
        if let button = view as? NSPopUpButton, button.menu === menu { return true }
        return view.subviews.contains { contains($0, menu) }
    }
}
