import Foundation
import KeryxKit

/// Where Local Models can be asked to scroll to and mark (`AppModel.modelsSpotlight`),
/// besides a catalog model by its name.
enum ModelsAnchor {
    static let modelServer = "model-server"
    static let voiceModels = "voice-models"
}

extension AppModel {
    /// Local Models, scrolled to `anchor` (a catalog model's name, or a `ModelsAnchor`) and
    /// marked a moment.
    func show(inModels anchor: String) {
        modelsSpotlight = anchor
        selection = .models
    }

    /// Local Models, at the model recommended for this Mac — or at its first model, when the
    /// catalog has not been read.
    func showRecommendedModel() {
        let models = self.models.value?.models ?? []
        let recommended = models.first { $0.recommended } ?? ModelsOrder.sorted(models).first
        show(inModels: recommended?.name ?? ModelsAnchor.modelServer)
    }
}

/// A refusal from `keryx models start` or `keryx voice use here` that is a step to take
/// first — said as guidance with the action that takes it, never as an error.
enum LocalRefusal: Equatable {
    /// There is no local model to serve, or for the voice's words: choose (and download) one.
    case needsModel
    /// The model server the voice's words come from isn't answering: start it first.
    case needsServer

    /// The refusal `message` is, when it is one of these.
    static func of(_ message: String) -> LocalRefusal? {
        let text = message.lowercased()
        if text.contains("set up a local model first") || text.contains("choose and download a model first") {
            return .needsModel
        }
        if text.contains("model server isn't running") || text.contains("model server is not running") {
            return .needsServer
        }
        return nil
    }
}

/// How Local Models says what its servers are doing.
@MainActor enum ModelsWording {
    /// The model server's state in a few words: "Running at 127.0.0.1:8090", "Not running".
    static func serverState(_ llm: LLMServer) -> String {
        llm.reachable ? "Running at \(Address.of(llm.url, port: llm.port))" : "Not running"
    }

    /// Why the model server cannot be started from here, or `nil` when it can: no model
    /// chosen for it, or the local agent's model served by something else (Ollama).
    static func cannotStart(_ servers: Servers) -> String? {
        guard servers.llm.model == nil else { return nil }
        guard servers.localAgent.model != nil, let base = servers.localAgent.baseUrl else {
            return "Choose a model below first."
        }
        return "The local agent's model is served at \(VoiceWording.address(base)), not by this Mac's model server."
    }

    /// Whether the speech models are on this Mac yet.
    static func speechModels(ready: Bool) -> String {
        ready ? "Downloaded" : "Not downloaded"
    }

    static let speechModelsNote = "Set Up in Voice downloads them too, or download them now."

    static let serverFailed = "The model server didn't start"
    static let downloadFailed = "The speech models didn't download"
    static let chooseModelFirst = "Choose a model first"
    static let chooseModelDetail = "Download one below and choose Use This Model; Start Model Server then runs it."

    /// The question before Stop Model Server, while this Mac's voice takes its words from it.
    static func stopQuestion(assistant: String?) -> (title: String, message: String) {
        let voice = assistant.map { "\($0)'s voice" } ?? "The voice"
        return ("Stop the model server?",
                "\(voice) on this Mac takes its words from it, and stops answering until it starts again.")
    }
}
