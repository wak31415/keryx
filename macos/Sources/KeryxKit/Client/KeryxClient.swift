import Foundation

/// Everything the app asks of Keryx. `CLIClient` answers it by running the `keryx` CLI with
/// `--json`; it is a protocol so a faster transport can take over a hot path later, and so
/// views can be previewed against a fake.
public protocol KeryxClient: Sendable {
    func status() async throws -> StatusReport
    /// `ok == false` is an answer, not an error: what is missing is in the checks.
    func doctor() async throws -> DoctorReport
    func tasks(includeInternal: Bool, limit: Int) async throws -> [TaskItem]
    func task(id: Int) async throws -> TaskDetail
    func usage(days: Int) async throws -> UsageReport
    func models() async throws -> ModelsReport
    /// Progress of a download, as it happens. `stop()` stops the download, and the events
    /// go on until it has (for the CLI, until `keryx models pull` has exited — normally
    /// after a `cancelled` event), so the caller knows nothing is still writing to it.
    /// Cancelling the consumer stops it too, without waiting.
    func pull(model: String) -> PullStream
    func use(model: String, defaultAgent: Bool) async throws -> UseResult
    /// Runs `CHECKOUT/scripts/install-launchd.sh --llm`, and returns what it printed.
    func installModelServer() async throws -> String
    func config() async throws -> [ConfigRow]
    /// What `key` may be set to. A live list that could not be read is still an answer
    /// (`live` false, `error` set); a key with no options throws.
    func options(key: String) async throws -> ConfigOptions
    func set(key: String, value: String) async throws
    /// The value goes on the child's stdin, never its argv or environment.
    func setSecret(key: String, value: String) async throws
    func unset(key: String) async throws
    func approvals(limit: Int) async throws -> ApprovalsReport
    func setEscalation(enabled: Bool) async throws
    func memory() async throws -> MemoryDocument
    func restart(force: Bool) async throws

    // MARK: Voice

    /// How the assistant speaks now (`keryx voice`).
    func voice() async throws -> VoiceStatus
    /// Speaks through OpenAI Realtime from the next restart.
    func useVoiceOpenAI() async throws -> VoiceUseResult
    /// Speaks through the voice server at `url` from the next restart, once it has answered
    /// a handshake — or, with `keepAnyway`, even if it did not. A failed handshake is an
    /// answer (`problem` set, nothing saved), not an error. `key` goes on the child's stdin.
    func useVoiceServer(url: String, key: String?, keepAnyway: Bool) async throws -> VoiceServerResult
    /// Installs, configures and starts the voice server on this Mac, step by step; stops
    /// as `pull` does. `voice`: the Kokoro voice to speak in, else the one set.
    func voiceHere(voice: String?) -> VoiceSetupStream
}
