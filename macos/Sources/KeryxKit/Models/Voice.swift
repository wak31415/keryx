import Foundation

/// `keryx voice --json`: how the assistant speaks now, and what each way needs.
public struct VoiceStatus: Codable, Sendable, Equatable {
    /// `openai`, `here` or `server`; see `kind`.
    public var mode: String
    /// The voice server the calls use; `nil` with OpenAI Realtime.
    public var baseUrl: String?
    public var realtimeModel: String
    public var voice: String
    /// The assistant's name ("Lyra").
    public var assistant: String
    public var openaiKeySet: Bool
    /// What must be set up before this Mac can speak for itself: `local_agent_model` while
    /// the voice shares the local agent's model and there is none, `voice_llm` while it has
    /// an LLM of its own (or elsewhere) with no model named. `nil` when nothing is.
    public var needs: String?
    public var here: HereVoice
    public var server: ElsewhereVoice
    /// Where the voice on this Mac takes its words from (`VOICE_LLM`); `nil` from a CLI
    /// older than a voice LLM of its own, whose words were always the local agent's.
    public var llm: VoiceLLMStatus?
    /// The ways each mode can speak — `speech-to-speech` (one model hears and answers) or
    /// `three-model` (hear, think, speak) — by mode (`openai`, `here`, `server`). `nil` from
    /// a CLI without it: each mode has its one way.
    public var pipelines: [String: [String]]?

    /// The mode, when this build knows it.
    public var kind: VoiceMode? { VoiceMode(rawValue: mode) }

    /// The pipelines `mode` offers, when the CLI says (`pipelines`); `nil` when it doesn't.
    public func pipelines(for mode: VoiceMode) -> [String]? { pipelines?[mode.rawValue] }

    /// This Mac's voice needs a local model first.
    public var needsLocalModel: Bool { needs == VoiceStatus.localAgentModel }

    /// This Mac's voice has an LLM of its own, or one elsewhere, with no model named.
    public var needsVoiceLLM: Bool { needs == VoiceStatus.voiceLLM }

    /// Where the words come from: the CLI's `llm.source`, else the local agent's model.
    public var llmSource: VoiceLLMSource { llm.flatMap { VoiceLLMSource(rawValue: $0.source) } ?? .shared }

    public static let localAgentModel = "local_agent_model"
    public static let voiceLLM = "voice_llm"
}

/// `voice --json` `llm`: where a voice on this Mac takes its words from.
public struct VoiceLLMStatus: Codable, Sendable, Equatable {
    /// `shared`, `here` or `remote` (`VOICE_LLM`); see `VoiceStatus.llmSource`.
    public var source: String
    /// The model it asks for; `nil` while none is named.
    public var model: String?
    /// Its address; `nil` while it has none.
    public var url: String?
    /// Whether it answers — a server of its own only while it serves its model; `nil` when
    /// it was not asked (a remote one while the voice is not here, or no address).
    public var reachable: Bool?
    /// The voice LLM's own server is installed as a service (`dev.keryx.voice-llm`).
    public var unitInstalled: Bool

    public init(source: String, model: String?, url: String?, reachable: Bool?, unitInstalled: Bool) {
        self.source = source
        self.model = model
        self.url = url
        self.reachable = reachable
        self.unitInstalled = unitInstalled
    }
}

/// Where a voice on this Mac takes its words from (`VOICE_LLM`).
public enum VoiceLLMSource: String, Sendable, CaseIterable, Identifiable {
    /// The local agent's model, as before the voice had one of its own.
    case shared
    /// A model of its own, on a llama.cpp server of its own on this Mac.
    case here
    /// A server elsewhere: a box of the owner's, or a cloud's Responses API.
    case remote

    public var id: String { rawValue }
}

/// What `keryx voice llm` is asked to make the voice's words.
public enum VoiceLLMChoice: Sendable, Equatable {
    /// `voice llm shared`: the local agent's model.
    case shared
    /// `voice llm here MODEL`: a catalog name (downloaded first when it is not) or a GGUF path.
    case here(model: String)
    /// `voice llm remote URL --model M`: its key kept, typed (on stdin) or removed, as a
    /// voice server's is.
    case remote(url: String, model: String, key: ServerKey)
}

/// `keryx voice llm … --json`'s `done`: where the words come from now, and what became of a
/// voice server running here.
public struct VoiceLLMChosen: StreamDone {
    /// `shared`, `here` or `remote`.
    public var source: String
    public var model: String?
    public var url: String?
    /// A voice server running here was restarted onto the new words, and answers.
    public var voiceRestarted: Bool
    /// The voice is here with no voice server running: it takes them up when it starts.
    public var voiceRestartNeeded: Bool

    public init(source: String, model: String?, url: String?, voiceRestarted: Bool, voiceRestartNeeded: Bool) {
        self.source = source
        self.model = model
        self.url = url
        self.voiceRestarted = voiceRestarted
        self.voiceRestartNeeded = voiceRestartNeeded
    }

    public init?(event: [String: Any]) {
        guard let source = event["source"] as? String,
              let restarted = MigrationDone.boolean(event["voice_restarted"]),
              let needed = MigrationDone.boolean(event["voice_restart_needed"]) else { return nil }
        self.init(source: source, model: event["model"] as? String, url: event["url"] as? String,
                  voiceRestarted: restarted, voiceRestartNeeded: needed)
    }

    public static let stoppedWithoutSaying = "Choosing the voice LLM stopped without saying why."
}

public typealias VoiceLLMEvent = StreamEvent<VoiceLLMChosen>
/// `keryx voice llm shared|here|remote --json`, which can be asked to stop.
public typealias VoiceLLMStream = StoppableStream<VoiceLLMEvent>

/// The three ways the assistant can speak.
public enum VoiceMode: String, Sendable, CaseIterable, Identifiable {
    /// OpenAI's Realtime API.
    case openai
    /// A voice server on this Mac.
    case here
    /// A voice server somewhere else.
    case server

    public var id: String { rawValue }
}

/// The voice server on this Mac.
public struct HereVoice: Codable, Sendable, Equatable {
    public var installed: Bool
    /// The voice server's program, when it is installed.
    public var program: String?
    /// The Kokoro voice it speaks in (`VOICE_SERVER_VOICE`).
    public var voice: String
    public var port: Int
    public var unitInstalled: Bool
    public var reachable: Bool
    /// The local model it puts the words in; `nil` while there is none.
    public var wordsModel: String?
    /// The speech-to-text model it hears with (`VOICE_SERVER_STT`).
    public var stt: String
    /// The text-to-speech model it speaks with (`VOICE_SERVER_TTS`).
    public var tts: String
    /// Both are already in the Hugging Face cache, so its first start downloads nothing
    /// (`keryx voice download` fetches them ahead).
    public var modelsReady: Bool
}

/// A voice server somewhere else.
public struct ElsewhereVoice: Codable, Sendable, Equatable {
    /// Whether it answered; `nil` when none is configured or it was not asked.
    public var reachable: Bool?
}

/// What `voice use server` does with the voice server's API key (`VOICE_API_KEY`). One of
/// the three, so `--key-stdin` and `--clear-key`, which the CLI refuses together, never are.
public enum ServerKey: Sendable, Equatable {
    /// Keep the key stored, if there is one: no flag.
    case stored
    /// Use this key, sent on stdin (`--key-stdin`). Blank, it is `stored`: the CLI refuses
    /// an empty stdin.
    case typed(String)
    /// Remove the stored key (`--clear-key`).
    case clear

    /// The key to send on stdin, trimmed; `nil` when there is none to send.
    public var toSend: String? {
        guard case let .typed(key) = self else { return nil }
        let trimmed = key.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}

/// `keryx voice use openai --json`.
public struct VoiceUseResult: Codable, Sendable, Equatable {
    /// What was written, each secret as "(set)" or null — never the value.
    public var saved: [String: JSONValue]
    public var restartNeeded: Bool

    public init(saved: [String: JSONValue], restartNeeded: Bool) {
        self.saved = saved
        self.restartNeeded = restartNeeded
    }
}

/// `keryx voice use server URL --json`. A server that failed the handshake saves nothing
/// (`saved` is `nil`) and says why in `problem`, unless it was kept anyway.
public struct VoiceServerResult: Codable, Sendable, Equatable {
    public var saved: [String: JSONValue]?
    public var problem: String?
    /// What the server or its socket said, on one line, when it opened no session: for Show
    /// Details only. Optional for an older CLI, whose `problem` said it itself.
    public var detail: String?
    public var restartNeeded: Bool
    /// What is worth knowing about the server even when it was saved.
    public var warnings: [String]

    public init(saved: [String: JSONValue]?, problem: String?, detail: String? = nil, restartNeeded: Bool,
                warnings: [String]) {
        self.saved = saved
        self.problem = problem
        self.detail = detail
        self.restartNeeded = restartNeeded
        self.warnings = warnings
    }
}

/// One line of `keryx voice use here --json`. Like `PullEvent`, parsed by hand: a line that
/// is not one of these is not an event.
public enum VoiceSetupEvent: Equatable, Sendable {
    /// A phase begins (`install`, `download`, `configure`, `unit`, `start`; a first set-up's
    /// voice LLM, `voice_llm`, before them). `progress` only
    /// when an older CLI nests sizes in the step; a current one says them in `.progress`.
    case step(step: String, message: String, progress: Progress?, detail: String? = nil)
    /// Bytes so far while the speech models download (`step` is `download`); `total` is
    /// `nil` when Hugging Face does not say.
    case progress(step: String, done: Int64, total: Int64?)
    case done(baseURL: String)
    case error(StreamFailure)
    case cancelled

    public struct Progress: Equatable, Sendable {
        public var done: Int64
        /// 0 when the size is not known.
        public var total: Int64

        public init(done: Int64, total: Int64) {
            self.done = done
            self.total = total
        }
    }

    /// What a setup that ended with none of `done`, `error` or `cancelled` says.
    public static let stoppedWithoutSaying =
        "The voice setup stopped without saying why."

    /// It ends the setup.
    public var isTerminal: Bool {
        switch self {
        case .step, .progress: false
        case .done, .error, .cancelled: true
        }
    }

    public static func parse(_ line: String) -> VoiceSetupEvent? {
        guard let data = line.data(using: .utf8),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let event = object["event"] as? String
        else { return nil }
        switch event {
        case "step":
            guard let step = object["step"] as? String, let message = object["message"] as? String else {
                return nil
            }
            var progress: Progress?
            if let sizes = object["progress"] as? [String: Any],
               let done = (sizes["done"] as? NSNumber)?.int64Value,
               let total = (sizes["total"] as? NSNumber)?.int64Value {
                progress = Progress(done: done, total: total)
            }
            return .step(step: step, message: message, progress: progress, detail: object["detail"] as? String)
        case "progress":
            guard let step = object["step"] as? String,
                  let done = object["done"] as? NSNumber, CFGetTypeID(done) != CFBooleanGetTypeID()
            else { return nil }
            let total = (object["total"] as? NSNumber).flatMap {
                CFGetTypeID($0) == CFBooleanGetTypeID() ? nil : $0.int64Value
            }
            return .progress(step: step, done: done.int64Value, total: total)
        case "done":
            guard let url = object["base_url"] as? String else { return nil }
            return .done(baseURL: url)
        case "error":
            return StreamFailure(event: object).map { .error($0) }
        case "cancelled":
            return .cancelled
        default:
            return nil
        }
    }
}

/// A voice setup's events, which can be asked to stop (`KeryxClient.voiceHere`).
public typealias VoiceSetupStream = StoppableStream<VoiceSetupEvent>
