import Foundation

/// Set Up… on Voice → This Mac, as one action: `keryx voice use here` refuses while the
/// model server its words come from isn't answering, so when it isn't, `keryx models start`
/// runs first (`AppModel.modelServerRun`) and the voice setup (`AppModel.voiceSetup`)
/// follows once it serves. This says which of the two the latest Set Up… began with.
public struct ThisMacSetup: Equatable, Sendable {
    /// It began by starting the model server.
    public var startsServer: Bool

    public init(startsServer: Bool) {
        self.startsServer = startsServer
    }
}

extension ModelsReport {
    /// This Mac's model server is meant to serve a model (`LLM_SERVER_MODEL` is set) and
    /// isn't answering: what `keryx models start` is for, and what `keryx voice use here`
    /// refuses over.
    public var modelServerDown: Bool {
        servers.llm.model != nil && !servers.llm.reachable
    }
}
