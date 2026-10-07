import Foundation

/// Everything that can go wrong talking to Keryx, each with words to show for it.
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

    /// Short, for a `ContentUnavailableView` title.
    public var title: String {
        switch self {
        case .notFound: "Keryx Not Found"
        case .migrationRequired: "Keryx Needs Migrating"
        case .liveCalls: "Calls in Progress"
        case .failed: "Keryx Refused"
        case .decoding: "Unexpected Answer"
        case .timedOut: "Keryx Did Not Answer"
        case .launch: "Keryx Could Not Start"
        }
    }

    /// The sentence to show.
    public var message: String {
        switch self {
        case let .notFound(failure):
            return (["Looked for Keryx and did not find it."] + failure.lookedAt).joined(separator: " ")
        case let .migrationRequired(sentence):
            return sentence
        case let .liveCalls(count):
            let calls = count == 1 ? "1 call is" : "\(count) calls are"
            return "\(calls) in progress, and a restart would cut \(count == 1 ? "it" : "them") off."
        case let .failed(command, exitCode, message):
            return message.isEmpty ? "\(command) stopped with exit status \(exitCode)." : message
        case let .decoding(command, detail):
            return "\(command) printed something this app could not read: \(detail)"
        case let .timedOut(command):
            return "\(command) took too long, and was stopped."
        case let .launch(message):
            return message
        }
    }
}
