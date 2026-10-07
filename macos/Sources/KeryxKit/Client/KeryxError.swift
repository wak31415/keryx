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

    /// What happened, and what to do, in sentences ending in a full stop. What Keryx
    /// printed is quoted, never reworded.
    public var message: String {
        switch self {
        case let .notFound(failure):
            return (["Looked for Keryx and didn't find it."] + failure.lookedAt.map(Self.sentence))
                .joined(separator: " ")
        case let .migrationRequired(sentence):
            return Self.sentence(sentence)
        case let .liveCalls(count):
            let calls = count == 1 ? "1 call is" : "\(count) calls are"
            return "\(calls) in progress, and restarting would end \(count == 1 ? "it" : "them")."
        case let .failed(command, exitCode, message):
            guard !message.isEmpty else {
                return "\(command) stopped with exit status \(exitCode). Run keryx doctor in Terminal to see why."
            }
            return "Keryx reported: “\(message)”."
        case let .decoding(command, _):
            return "This app couldn't read what \(command) printed. Update the app and Keryx to the same version."
        case let .timedOut(command):
            return "\(command) took too long and was stopped. Try again in a moment."
        case let .launch(message), let .terminal(message), let .refused(message):
            return Self.sentence(message)
        }
    }

    /// What the CLI itself printed for a refused command, unquoted — for a screen that
    /// rewords the sentences it knows (a setting's validation). `nil` for anything else.
    public var reported: String? {
        if case let .failed(_, _, message) = self, !message.isEmpty { return message }
        return nil
    }

    /// `text` with a capital first letter and a closing full stop.
    public static func sentence(_ text: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = trimmed.first else { return trimmed }
        let capitalised = first.uppercased() + trimmed.dropFirst()
        return ".!?…".contains(capitalised.last!) ? capitalised : capitalised + "."
    }
}
