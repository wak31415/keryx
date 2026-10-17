import Foundation

// SEAMS: what the app is built to use before the CLI has it, so the two can land in either
// order. Each is a small protocol here, which a client may adopt (the app asks with `as?`
// and does without otherwise), or an optional field on a document model, listed in
// `ContractTests.awaitingCLI` until the contract names it. Nothing is awaited today;
// `AgentSmokeTesting` stays a protocol of its own so a preview or test client need not run
// an agent.

/// A coding agent asked to run a test task, as a stream (`keryx auth smoke NAME
/// --json`, NAME `claude`, `codex` or `local`: `step`, then `done {ok, summary, detail}`,
/// `error` or `cancelled`), so the wizard can show it running. A `done` that did not pass
/// exits 1 and is still the ending (the contract's `done` exit is `[0, 1]`). `CLIClient`
/// adopts it with `events(["auth", "smoke", name, "--json"])`.
public protocol AgentSmokeTesting: KeryxClient {
    func smokeTest(_ agent: String) -> AgentSmokeStream
}

/// `keryx auth smoke NAME --json`'s `done`.
public struct AgentSmoked: StreamDone {
    /// The agent answered as asked.
    public var ok: Bool
    /// What it said, or why not, in a sentence.
    public var summary: String
    /// What the agent said, or the run's error, for Show Details; `nil` when it passed.
    public var detail: String?

    public init(ok: Bool, summary: String, detail: String? = nil) {
        self.ok = ok
        self.summary = summary
        self.detail = detail
    }

    public init?(event: [String: Any]) {
        guard let ok = MigrationDone.boolean(event["ok"]), let summary = event["summary"] as? String else {
            return nil
        }
        self.init(ok: ok, summary: summary, detail: event["detail"] as? String)
    }

    public static let stoppedWithoutSaying = "The agent's test stopped without saying why."
}

public typealias AgentSmokeStream = StoppableStream<StreamEvent<AgentSmoked>>
