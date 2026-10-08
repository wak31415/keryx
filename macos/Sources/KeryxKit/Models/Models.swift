import Foundation

/// `keryx models list --json`: the catalog, this machine's hardware, and the local servers.
public struct ModelsReport: Codable, Sendable, Equatable {
    public var hardware: Hardware
    public var modelsDir: String
    public var models: [CatalogModel]
    public var voices: [Voice]
    public var servers: Servers
    /// SEAM (`models list --json`, not printed yet): the catalog key recommended for the
    /// local agent on this Mac, with the owner's rule for the voice's own model applied by
    /// the CLI. `nil` from a CLI without it: the catalog's own `recommended` flag stands in.
    public var recommended: String?
    /// SEAM: the same, when the voice runs on this Mac too and takes its share of memory.
    public var recommendedWithLocalVoice: String?
    /// SEAM: the small model recommended for the voice's words (`VOICE_LLM_MODEL`).
    public var voiceLlmRecommended: String?
    /// SEAM: whether the voice's words model fits beside the agent's (the owner's rule), so
    /// it runs separately rather than sharing the agent's.
    public var voiceLlmSeparate: Bool?
}

/// Mirrors `dataclasses.asdict(Hardware)` from `src/keryx/localmodels/hardware.py`, plus
/// `model_memory_gb`, which is a computed property on the Python side and so is added to the
/// dict by hand rather than appearing in `asdict`'s own fields.
public struct Hardware: Codable, Sendable, Equatable {
    public var accelerator: String
    public var gpu: String?
    public var vramGb: Double
    public var ramGb: Double
    public var modelMemoryGb: Double
}

public struct CatalogModel: Codable, Sendable, Equatable, Identifiable {
    public var name: String
    public var title: String
    public var note: String
    public var sizeGb: Double
    public var needsGb: Double
    public var fits: Bool
    public var downloaded: Bool
    public var recommended: Bool
    public var tested: Bool
    /// SEAM (`models list --json` `models[].fits_with_voice_llm`, not printed yet): it fits
    /// beside a voice LLM of its own and the speech models, as the CLI works it out. `nil`
    /// from a CLI without it.
    public var fitsWithVoiceLlm: Bool?

    public var id: String { name }
}

public struct Voice: Codable, Sendable, Equatable, Identifiable {
    public var name: String
    public var sounds: String

    public var id: String { name }
}

public struct Servers: Codable, Sendable, Equatable {
    public var llm: LLMServer
    public var voice: VoiceServer
    public var localAgent: LocalAgent
}

public struct LLMServer: Codable, Sendable, Equatable {
    /// The model the settings name (`AGENT_SERVER_MODEL`): what the server loads when it
    /// (re)starts.
    public var model: String?
    /// The model the running server says it is serving; `nil` when it is not reachable or
    /// did not say. Differs from `model` after `keryx models use` until a restart.
    public var servedModel: String?
    public var port: Int
    public var url: String?
    public var binary: String?
    public var unitInstalled: Bool
    public var reachable: Bool
}

public struct VoiceServer: Codable, Sendable, Equatable {
    public var url: String?
    public var port: Int
    public var unitInstalled: Bool
    public var reachable: Bool
}

public struct LocalAgent: Codable, Sendable, Equatable {
    public var baseUrl: String?
    public var model: String?
    public var api: String
    public var enabled: Bool
    public var `default`: Bool
}

/// One line of `keryx models pull NAME --json`. Not `Codable`: the wire format is a loose,
/// evolving set of shapes (`{"event": "progress", ...}` and friends), and `parse` is the one
/// place that needs to know any of them — a non-JSON line, or JSON missing the fields its
/// event needs, is simply not a `PullEvent` rather than a decoding error to propagate.
public enum PullEvent: Equatable, Sendable {
    case progress(done: Int64, total: Int64)
    case done(path: String)
    case adopted(path: String, from: String)
    case error(StreamFailure)
    case cancelled

    /// What a pull that ended with none of `done`, `adopted`, `error` or `cancelled` says.
    public static let stoppedWithoutSaying =
        "The download stopped without saying why."

    public static func parse(_ line: String) -> PullEvent? {
        guard let data = line.data(using: .utf8),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let event = object["event"] as? String
        else { return nil }
        switch event {
        case "progress":
            guard let done = (object["done"] as? NSNumber)?.int64Value,
                  let total = (object["total"] as? NSNumber)?.int64Value
            else { return nil }
            return .progress(done: done, total: total)
        case "done":
            guard let path = object["path"] as? String else { return nil }
            return .done(path: path)
        case "adopted":
            guard let path = object["path"] as? String, let from = object["from"] as? String else {
                return nil
            }
            return .adopted(path: path, from: from)
        case "error":
            return StreamFailure(event: object).map { .error($0) }
        case "cancelled":
            return .cancelled
        default:
            return nil
        }
    }
}

/// A download's events, which can be asked to stop (`KeryxClient.pull`).
public typealias PullStream = StoppableStream<PullEvent>

/// `keryx models use NAME --json`.
public struct UseResult: Codable, Sendable, Equatable {
    public var saved: [String: JSONValue]
    public var llamaServer: String?
    public var restartNeeded: Bool
}
