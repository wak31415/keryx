import Foundation

/// Set Up… on Voice → This Mac, as one action: `keryx voice use here` refuses while the
/// model server its words come from isn't answering, so when it isn't, `keryx models start`
/// runs first (`AppModel.modelServerRun`) and the voice setup (`AppModel.voiceSetup`)
/// follows once it serves. This says which of the two the latest Set Up… began with.
public struct ThisMacSetup: Equatable, Sendable {
    /// It began by starting the model server.
    public var startsServer: Bool
    /// The model server's runtime, when it is not llama.cpp (`ollama`).
    public var runtime: String?

    public init(startsServer: Bool, runtime: String? = nil) {
        self.startsServer = startsServer
        self.runtime = runtime
    }
}

/// What serves the model the local agent (and this Mac's voice) takes its words from.
public enum WordsServer: Equatable, Sendable {
    /// This Mac's llama.cpp model server (`LLM_SERVER_MODEL` is set): `keryx models start`.
    case modelServer
    /// Ollama on this Mac: `keryx models start --runtime ollama`.
    case ollama
    /// Something else, at this address, which Keryx does not start.
    case elsewhere(String)
    /// No local model at all.
    case noModel
}

extension ModelsReport {
    /// This Mac's model server is meant to serve a model (`LLM_SERVER_MODEL` is set) and
    /// isn't answering: what `keryx models start` is for, and what `keryx voice use here`
    /// refuses over.
    public var modelServerDown: Bool {
        servers.llm.model != nil && !servers.llm.reachable
    }

    /// What serves the local agent's model: this Mac's model server, Ollama on this Mac
    /// (its own port on loopback), or something else.
    public var wordsServer: WordsServer {
        if servers.llm.model != nil { return .modelServer }
        guard let base = servers.localAgent.baseUrl else { return .noModel }
        if let parts = URLComponents(string: base), let host = parts.host,
           ["127.0.0.1", "localhost", "::1", "[::1]"].contains(host), parts.port == Self.ollamaPort {
            return .ollama
        }
        return .elsewhere(base)
    }

    /// Ollama's port, as Keryx points the local agent at it.
    static let ollamaPort = 11434
}
