import Foundation

/// Everything that can go wrong talking to Keryx, each with words to show for it: a title
/// that says what happened, and a message that says it in a sentence and what to do next.
public enum KeryxError: Error, Equatable, Sendable {
    /// The locator found no `keryx` to run.
    case notFound(LocateFailure)
    /// The CLI's own sentence after `keryx cannot do that yet: ` — `keryx migrate` has yet
    /// to run.
    case migrationRequired(String)
    /// `keryx restart` refused: this many live sessions would be cut off.
    case liveCalls(Int)
    /// `message` is the trimmed stderr, else the trimmed stdout.
    case failed(command: String, exitCode: Int32, message: String)
    case decoding(command: String, detail: String)
    case timedOut(command: String)
    case launch(String)
    /// Terminal could not be handed a command to run.
    case terminal(String)
    /// The app would not do it, and ran nothing: what it was given cannot be used (a line
    /// break in a key, an address it will not open). The sentence says why.
    case refused(String)

    /// Short, in title case, for an alert or a `ContentUnavailableView`.
    public var title: String {
        switch self {
        case .notFound: "Keryx Isn't on This Mac"
        case .migrationRequired: "Keryx Needs to Move Its Files"
        case .liveCalls: "Calls in Progress"
        case .failed: "Keryx Reported a Problem"
        case .decoding: "Unexpected Answer from Keryx"
        case .timedOut: "Keryx Didn't Respond"
        case .launch: "Keryx Couldn't Start"
        case .terminal: "Couldn't Open Terminal"
        case .refused: "Can't Do That"
        }
    }

    /// The title for a screen that could not load `section` ("Sessions"): what failed to
    /// load, unless the error itself says more than that (Keryx did not answer, or could
    /// not start).
    public func title(loading section: String) -> String {
        switch self {
        case .failed, .decoding: "Couldn't Load \(section)"
        default: title
        }
    }

    /// What happened, and what to do, in sentences ending in a full stop — always the
    /// app's own words: what Keryx printed is never the sentence, only its `details`.
    public var message: String {
        switch self {
        case .notFound:
            return "Looked for Keryx and didn't find it."
        case .migrationRequired:
            return "Keryx's files are still where Jarvis kept them, and move before it runs again."
        case let .liveCalls(count):
            let calls = count == 1 ? "1 call is" : "\(count) calls are"
            return "\(calls) in progress, and restarting would end \(count == 1 ? "it" : "them")."
        case let .failed(_, _, message):
            if message.isEmpty { return "Keryx stopped without saying why." }
            return isOlderKeryx ? Self.olderKeryx : "Keryx couldn't do that."
        case let .decoding(command, _):
            return "This app couldn't read what \(command) printed. Update the app and Keryx to the same version."
        case .timedOut:
            return "Keryx took too long to answer and was stopped. Try again in a moment."
        case .launch:
            return "Keryx couldn't be started."
        case .terminal:
            return "The command couldn't be opened in Terminal."
        case let .refused(message):
            return Self.sentence(message)
        }
    }

    /// What every screen says when the CLI does not know the command or an option the app
    /// asked for.
    public static let olderKeryx = "The installed Keryx is older than this app."

    /// The CLI did not know the command or an option: a Keryx from before the app's.
    public var isOlderKeryx: Bool {
        guard case let .failed(_, exitCode, message) = self else { return false }
        let markers = ["No such command", "No such option", "Got unexpected extra argument", "no such option"]
        return markers.contains(where: message.contains) || (exitCode == 2 && message.hasPrefix("Usage:"))
    }

    /// The sentence a screen says for its own operation ("Slack wasn't turned on."), and what
    /// Keryx printed under Show Details: a refusal's own text is never the sentence. A Keryx
    /// older than the app, a timeout or a launch failure keeps its own sentence.
    public func said(_ operation: String) -> StreamFailure {
        if case let .failed(_, _, message) = self, !message.isEmpty, !isOlderKeryx {
            return StreamFailure(operation, detail: message)
        }
        return StreamFailure(self)
    }

    /// What a person might pass on but should not have to read: what Keryx printed when it
    /// is not a sentence for the app (a command, a path, a traceback), the exit status of a
    /// command that said nothing, what could not be decoded. For Show Details only; `nil`
    /// when the message says it all.
    public var details: String? {
        switch self {
        case let .notFound(failure):
            return failure.lookedAt.isEmpty ? nil : failure.lookedAt.joined(separator: "\n")
        case let .migrationRequired(sentence), let .launch(sentence), let .terminal(sentence):
            return sentence.isEmpty ? nil : sentence
        case let .failed(command, exitCode, message):
            if message.isEmpty { return "\(command) exited with status \(exitCode)." }
            return message
        case let .decoding(command, detail):
            return detail.isEmpty ? nil : "\(command): \(detail)"
        case let .timedOut(command):
            return "\(command) did not finish in time."
        case .liveCalls, .refused:
            return nil
        }
    }

    /// What the CLI itself printed for a refused command, unquoted — for a screen that
    /// rewords the sentences it knows (a setting's validation). `nil` for anything else.
    public var reported: String? {
        if case let .failed(_, _, message) = self, !message.isEmpty { return message }
        return nil
    }

    /// `text` with a capital first letter and a closing full stop — unless its first word is
    /// a program's or a host's name ("llama.cpp", "studio.local:8000"), which keeps its case.
    public static func sentence(_ text: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = trimmed.first else { return trimmed }
        let word = trimmed.prefix { !$0.isWhitespace && $0 != ":" && $0 != "," }
        let named = word.trimmingCharacters(in: CharacterSet(charactersIn: ".")).contains { "-_.".contains($0) }
        let capitalised = named ? trimmed : first.uppercased() + trimmed.dropFirst()
        return ".!?…".contains(capitalised.last!) ? capitalised : capitalised + "."
    }
}

/// Whether a line is fit to be the app's own copy: no shell command, no path, no traceback,
/// no second line. The CLI holds `detail` and `why` to the same rule (`doctor.app_facing`);
/// anything else goes under Show Details.
public enum AppFacing {
    static let shellText = ["`", "keryx ", "brew ", "uv ", "scripts/", "/login", "Traceback", "$ "]

    public static func isFit(_ text: String) -> Bool {
        if text.contains(where: \.isNewline) { return false }
        if shellText.contains(where: text.contains) { return false }
        // A word that is a path: /Users/…, ~/…, ./…
        return !text.split(whereSeparator: \.isWhitespace).contains { word in
            let token = word.drop { "(\"'“‘[".contains($0) }
            return token.hasPrefix("/") && token.count > 1 || token.hasPrefix("~/") || token.hasPrefix("./")
        }
    }
}

extension StreamFailure {
    /// A `KeryxError` as a run's failure: its sentence, and what it would add under Show
    /// Details.
    public init(_ error: KeryxError) {
        self.init(error.message, detail: error.details)
    }
}
