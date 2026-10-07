import Foundation

/// One line of a long-running `keryx … --json` command that is not a model pull: `models
/// start`, `voice download` and the four `auth login NAME`. They share one vocabulary — a
/// `step` before each phase's slow work, `progress` in bytes while something downloads,
/// `open` and `code` while a sign-in waits on the owner — and end with exactly one `done`
/// (its payload, `Done`, is each command's own), `error` or `cancelled`.
///
/// Like `PullEvent`, parsed by hand: a line that is not one of these is not an event, and a
/// key that is missing makes it none too, except the ones said only sometimes (`total`,
/// Codex's `code`, Claude's `stored`).
public enum StreamEvent<Done: StreamDone>: Equatable, Sendable {
    /// A phase begins (or a warning within it): `step` is the CLI's name for it. `detail`
    /// is what a terminal adds (a path, a command), never shown as the line itself.
    case step(step: String, message: String, detail: String? = nil)
    /// Bytes so far in `step`; `total` is `nil` when the source does not say.
    case progress(step: String, done: Int64, total: Int64?)
    /// Open `url` in the browser. `code` is what the owner types there (Codex's device code).
    case open(url: String, code: String?)
    /// The command waits for one line on its stdin: the code the owner was shown (Claude).
    case code
    case done(Done)
    case error(StreamFailure)
    case cancelled

    /// It ends the command.
    public var isTerminal: Bool {
        switch self {
        case .done, .error, .cancelled: true
        case .step, .progress, .open, .code: false
        }
    }

    /// The event's name on the wire.
    public var name: String {
        switch self {
        case .step: "step"
        case .progress: "progress"
        case .open: "open"
        case .code: "code"
        case .done: "done"
        case .error: "error"
        case .cancelled: "cancelled"
        }
    }

    public static func parse(_ line: String) -> StreamEvent? {
        guard let data = line.data(using: .utf8),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let event = object["event"] as? String
        else { return nil }
        switch event {
        case "step":
            guard let step = object["step"] as? String, let message = object["message"] as? String else {
                return nil
            }
            return .step(step: step, message: message, detail: object["detail"] as? String)
        case "progress":
            guard let step = object["step"] as? String, let done = integer(object["done"]) else { return nil }
            return .progress(step: step, done: done, total: integer(object["total"]))
        case "open":
            guard let url = object["url"] as? String else { return nil }
            return .open(url: url, code: object["code"] as? String)
        case "code":
            return .code
        case "done":
            return Done(event: object).map { .done($0) }
        case "error":
            return StreamFailure(event: object).map { .error($0) }
        case "cancelled":
            return .cancelled
        default:
            return nil
        }
    }

    /// A JSON integer (never a boolean, which `NSNumber` would also give).
    private static func integer(_ value: Any?) -> Int64? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        return number.int64Value
    }
}

/// How a long-running command went wrong: `message`, one sentence for a person, said as it
/// is; `detail`, what a terminal would add (a tool's output, a path, a log), shown only
/// under Show Details. A string literal is a failure with no detail.
public struct StreamFailure: Equatable, Sendable, ExpressibleByStringLiteral, CustomStringConvertible {
    public var message: String
    public var detail: String?

    public init(_ message: String, detail: String? = nil) {
        self.message = message
        let trimmed = detail?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.detail = trimmed?.isEmpty == false ? trimmed : nil
    }

    public init(stringLiteral value: String) {
        self.init(value)
    }

    /// From an `error` event's keys: `message` it must have; `detail` it may (null, or
    /// missing from a CLI older than it).
    public init?(event: [String: Any]) {
        guard let message = event["message"] as? String else { return nil }
        self.init(message, detail: event["detail"] as? String)
    }

    public var description: String { message }
}

/// What a stream's `done` event carries.
public protocol StreamDone: Equatable, Sendable {
    /// From the `done` event's keys; `nil` when one it needs is missing.
    init?(event: [String: Any])
    /// What a stream that ended with none of `done`, `error` or `cancelled` says.
    static var stoppedWithoutSaying: String { get }
}

/// `keryx models start --json`'s `done`: the model server answers at `url` and serves
/// `servedModel`.
public struct ModelServerStarted: StreamDone {
    public var url: String
    public var servedModel: String

    public init(url: String, servedModel: String) {
        self.url = url
        self.servedModel = servedModel
    }

    public init?(event: [String: Any]) {
        guard let url = event["url"] as? String, let served = event["served_model"] as? String else { return nil }
        self.init(url: url, servedModel: served)
    }

    public static let stoppedWithoutSaying =
        "Starting the model server stopped without saying why."
}

/// `keryx voice download --json`'s `done`: the speech models are in the Hugging Face cache.
public struct VoiceModelsDownloaded: StreamDone {
    /// The model that hears (`VOICE_SERVER_STT`).
    public var stt: String
    /// The model that speaks (`VOICE_SERVER_TTS`).
    public var tts: String

    public init(stt: String, tts: String) {
        self.stt = stt
        self.tts = tts
    }

    public init?(event: [String: Any]) {
        guard let stt = event["stt"] as? String, let tts = event["tts"] as? String else { return nil }
        self.init(stt: stt, tts: tts)
    }

    public static let stoppedWithoutSaying =
        "The speech model download stopped without saying why."
}

/// `keryx auth login NAME --json`'s `done`.
public struct SignedIn: StreamDone {
    /// The sign-in's name (`claude`, `codex`, `gmail`, `google-workspace`).
    public var name: String
    /// What happened, in a sentence, said as it is.
    public var message: String
    /// The setting the sign-in stored (Claude's `CLAUDE_CODE_OAUTH_TOKEN`); `nil` for the
    /// others, whose tokens are their own files.
    public var stored: String?

    public init(name: String, message: String, stored: String? = nil) {
        self.name = name
        self.message = message
        self.stored = stored
    }

    public init?(event: [String: Any]) {
        guard let name = event["name"] as? String, let message = event["message"] as? String else { return nil }
        self.init(name: name, message: message, stored: event["stored"] as? String)
    }

    public static let stoppedWithoutSaying =
        "The sign-in stopped without saying why."
}

public typealias ModelServerEvent = StreamEvent<ModelServerStarted>
public typealias VoiceDownloadEvent = StreamEvent<VoiceModelsDownloaded>
public typealias SignInEvent = StreamEvent<SignedIn>

/// `keryx models start --json`, which can be asked to stop.
public typealias ModelServerStream = StoppableStream<ModelServerEvent>
/// `keryx voice download --json`, which can be asked to stop.
public typealias VoiceDownloadStream = StoppableStream<VoiceDownloadEvent>
/// `keryx auth login NAME --json`, which can be asked to stop and takes a line (`send`).
public typealias SignInStream = StoppableStream<SignInEvent>
