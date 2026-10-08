import Foundation

// SEAMS: what the setup wizard is built to use once the CLI has it, and does without until
// then. Each is one small protocol here, or one optional field on a document model (marked
// "SEAM" where it is declared, and listed in `ContractTests.awaitingCLI`):
//
// - `AgentSmokeTesting` — `keryx auth smoke NAME --json`.
// - `VoiceWordsServing` — `keryx models start --voice-llm --json`, the voice's own model.
// - `VoiceStatus.pipelines` — `voice --json` `pipelines`.
// - `ModelsReport.recommended`, `.recommendedWithLocalVoice`, `.voiceLlmRecommended`,
//   `.voiceLlmSeparate` — `models list --json`, the owner's rule applied by the CLI.
// - `PhoneTunnel.hostname` — `phone tunnel --json` `hostname`.
// - `SetupStep.deferrable`, `.deferred`, `SetupMarkResult.deferred` — `setup plan --json`,
//   and `setup mark phone|service`.
// - `config options ASSISTANT_NAME` — the `ASSISTANT_NAME` row's `has_options`, already read.
// - `VOICE_LLM`, `VOICE_LLM_MODEL` — settings, read from `config list` when they are there.
//
// No `KeryxClient` adopts the two protocols yet: each says how its command is run once the
// CLI prints it, and the contract names the command in the same change.

/// SEAM: a coding agent asked to run a test task, as a stream (Task L: `keryx auth smoke
/// NAME --json`, NAME `claude`, `codex` or `local`: `step`, then `done {ok, summary,
/// detail}`, `error` or `cancelled`), so the wizard can show it running. A `done` that did
/// not pass exits 1 and is still the ending (the contract's `done` exit is `[0, 1]`). Until
/// a client adopts it, whether an agent is ready is the doctor's sign-in check. A
/// `CLIClient` adopts it with `events(["auth", "smoke", name, "--json"])`.
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

/// SEAM: the voice's own small model, served by a second llama.cpp unit (`keryx models start
/// --voice-llm --json`, the same stream as `models start`), for when the voice and the agent
/// both run on this Mac with memory for two (`VOICE_LLM` `here`). Until a client adopts it,
/// the voice shares the agent's model, as it does today. A `CLIClient` adopts it with
/// `events(["models", "start", "--voice-llm", "--json"])`.
public protocol VoiceWordsServing: KeryxClient {
    func startVoiceWordsServer() -> ModelServerStream
}
