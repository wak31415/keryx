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
    func config() async throws -> [ConfigRow]
    /// What `key` may be set to. A live list that could not be read is still an answer
    /// (`live` false, `error` set); a key with no options throws.
    func options(key: String) async throws -> ConfigOptions
    func set(key: String, value: String) async throws
    /// The value goes on the child's stdin, never its argv or environment.
    func setSecret(key: String, value: String) async throws
    func unset(key: String) async throws
    /// Removes several settings in one `config unset K1 K2 …`.
    func unset(keys: [String]) async throws
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
    /// answer (`problem` set, nothing saved), not an error. A typed key goes on the child's
    /// stdin; `.clear` removes the stored one.
    func useVoiceServer(url: String, key: ServerKey, keepAnyway: Bool) async throws -> VoiceServerResult
    /// Installs, configures and starts the voice server on this Mac, step by step; stops
    /// as `pull` does. `voice`: the Kokoro voice to speak in, else the one set.
    func voiceHere(voice: String?) -> VoiceSetupStream

    // MARK: Setup

    /// The steps to a first working call (`keryx setup plan`). Always an answer: settings
    /// that will not load fail every step, with the reason.
    func setupPlan() async throws -> SetupPlan
    /// Records `step` as walked: an optional one skipped, or `pin` left to the first call.
    /// A required step other than `pin` is refused.
    func markSetupStep(_ step: String) async throws -> SetupMarkResult
    /// Whether a phone PIN is set and where — never the digits.
    func pin() async throws -> PinStatus
    /// Sets the phone PIN, or with `replace` replaces it. The digits go on the child's stdin,
    /// twice (the CLI compares them), never its argv; the caller has compared them already.
    func setPin(_ pin: String, replace: Bool) async throws -> PinSetResult
    /// Checks `value` for `key` before it is saved, saving nothing (`OPENAI_API_KEY`,
    /// `ANTHROPIC_API_KEY`, `CODEX_API_KEY`, `TWILIO_AUTH_TOKEN`); the value goes on stdin.
    /// Not working is an answer, not an error.
    func verify(key: String, value: String) async throws -> VerifyResult
    /// Checks a Twilio account SID and auth token together, both unsaved, on stdin.
    func verifyTwilio(accountSID: String, authToken: String) async throws -> VerifyResult

    // MARK: Phone

    /// The Twilio account's numbers. Twilio refusing is the report's `error`.
    func phoneNumbers() async throws -> PhoneNumbers
    /// Where `TWILIO_NUMBER`'s calls go now, and where they should.
    func phoneWebhooks() async throws -> PhoneWebhooks
    /// Points `TWILIO_NUMBER`'s two webhooks here: the one write to Twilio, which the
    /// caller has shown the owner (both addresses) and had a yes for.
    func applyPhoneWebhooks() async throws -> WebhooksApplied
    /// The tunnel, its unit, and whether Keryx answers at `PUBLIC_HOST`.
    func phoneTunnel() async throws -> PhoneTunnel

    // MARK: Service

    /// Each of Keryx's units, installed and running or not.
    func service() async throws -> ServiceReport
    /// Installs Keryx and its tunnel as services (never a model server's unit: those are
    /// `startModelServer`'s and `voiceHere`'s).
    func installService() async throws -> ServiceResult
    /// Stops and removes Keryx and its tunnel (never a model server's unit: that is
    /// `stopModelServer`'s).
    func uninstallService() async throws -> ServiceResult
    /// Runs the chosen model's server here (`keryx models start`), step by step: installs
    /// llama.cpp when missing, its unit, waits for it, checks what it serves. Stops as
    /// `pull` does. `runtime`: `llama` (the default) or `ollama`.
    func startModelServer(runtime: String?) -> ModelServerStream
    /// Stops the model server and removes its unit (`keryx models stop`).
    func stopModelServer() async throws -> ServiceResult
    /// Downloads the voice server's speech models ahead of its first start; stops as `pull`
    /// does.
    func downloadVoiceModels() -> VoiceDownloadStream

    // MARK: Accounts

    /// Signs in to `provider`, as a stream that takes input: `open` (open its link),
    /// Claude's `code` (send the code the owner was shown), Gmail's address (send the
    /// address the browser landed on, after `open`). `clientFile`: Google's OAuth client
    /// JSON, the first time (`gmail`, `google-workspace`). Stops as `pull` does.
    func signIn(_ provider: SignInProvider, clientFile: String?) -> SignInStream
    /// Signs out of `gmail` or `google-workspace`: their stored tokens go.
    func signOut(_ provider: SignInProvider) async throws -> SignOutResult
    /// The accounts Keryx connects to, and whether each is.
    func integrations() async throws -> IntegrationsReport

    // MARK: Plugins

    func plugins() async throws -> PluginsReport
    /// Turns a plugin on, writing `values` into its settings file first.
    func installPlugin(_ name: String, values: PluginValues) async throws -> PluginResult
    /// Turns a plugin off; its settings are kept.
    func removePlugin(_ name: String) async throws -> PluginResult
    /// Changes an installed plugin's settings (`settings`, `clusters`; never a guard).
    func setPlugin(_ name: String, values: PluginValues) async throws -> PluginResult
    /// The owner's own voice tools, and any the next call would refuse.
    func tools() async throws -> ToolsReport
    /// The ssh hosts `cluster_stats` could ask.
    func sshHosts() async throws -> SshHostsReport
}

extension KeryxClient {
    /// One `unset(key:)` after another, for a client with no single command for several.
    public func unset(keys: [String]) async throws {
        for key in keys { try await unset(key: key) }
    }
}
