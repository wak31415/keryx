import Foundation
import KeryxKit

/// A `KeryxClient` that answers from `PreviewData`, for SwiftUI previews and snapshots.
/// Every document can be replaced (an empty list, a `KeryxError`, the migration status);
/// writes succeed unless `writeError` says otherwise; and a pull replays `pull.jsonl` or
/// holds at a progress value until it is cancelled. The `StreamEvent` streams (`models
/// start`, `voice download`, a sign-in) follow a `Script` each.
public struct PreviewClient: KeryxClient {
    /// What one of the `StreamEvent` streams does.
    public enum Script<Done: StreamDone>: Sendable {
        /// Yields the events of its fixture (`PreviewData.modelsStartLines`, …), then finishes.
        case replay
        /// Yields these events, then waits until it is stopped (and says `cancelled`).
        case holding([StreamEvent<Done>])
        /// Yields these events, then finishes.
        case events([StreamEvent<Done>])
        /// Yields these events, and stops only long after it is asked to.
        case slowToStop([StreamEvent<Done>])
        /// Yields the first events, then waits for a line on its input (a sign-in's code or
        /// address), and then yields the second and finishes.
        case answering([StreamEvent<Done>], then: [StreamEvent<Done>])
    }

    /// What `pull(model:)` does.
    public enum Pull: Sendable {
        /// Yields the events of `PreviewData.pullLines`, then finishes.
        case replay
        /// Yields one progress event, then waits until the download is stopped.
        case holding(done: Int64, total: Int64)
        /// Yields these events, then finishes.
        case events([PullEvent])
        /// Yields one progress event, and stops only long after it is asked to: a download
        /// caught stopping.
        case slowToStop(done: Int64, total: Int64)
    }

    /// What `voiceHere(voice:)` does.
    public enum Setup: Sendable {
        /// Yields the events of `PreviewData.voiceHereLines`, then finishes.
        case replay
        /// Yields these events, then waits until the setup is stopped.
        case holding([VoiceSetupEvent])
        /// Yields these events, then finishes.
        case events([VoiceSetupEvent])
        /// Yields these events, and stops only long after it is asked to.
        case slowToStop([VoiceSetupEvent])
    }

    let statusReply: Result<StatusReport, KeryxError>
    let doctorReply: Result<DoctorReport, KeryxError>
    let tasksReply: Result<[TaskItem], KeryxError>
    let taskReply: Result<TaskDetail, KeryxError>
    let usageReply: Result<UsageReport, KeryxError>
    let modelsReply: Result<ModelsReport, KeryxError>
    let configReply: Result<[ConfigRow], KeryxError>
    let approvalsReply: Result<ApprovalsReport, KeryxError>
    let memoryReply: Result<MemoryDocument, KeryxError>
    let voiceReply: Result<VoiceStatus, KeryxError>
    let optionsReply: [String: Result<ConfigOptions, KeryxError>]
    let serverReply: Result<VoiceServerResult, KeryxError>
    let pullBehaviour: Pull
    let setupBehaviour: Setup
    let writeError: KeryxError?
    let setupPlanReply: Result<SetupPlan, KeryxError>
    let pinReply: Result<PinStatus, KeryxError>
    let serviceReply: Result<ServiceReport, KeryxError>
    let phoneNumbersReply: Result<PhoneNumbers, KeryxError>
    let phoneWebhooksReply: Result<PhoneWebhooks, KeryxError>
    let phoneTunnelReply: Result<PhoneTunnel, KeryxError>
    let integrationsReply: Result<IntegrationsReport, KeryxError>
    let pluginsReply: Result<PluginsReport, KeryxError>
    let toolsReply: Result<ToolsReport, KeryxError>
    let sshHostsReply: Result<SshHostsReport, KeryxError>
    let verifyReply: Result<VerifyResult, KeryxError>
    let modelServerScript: Script<ModelServerStarted>
    let voiceLLMScript: Script<VoiceLLMChosen>
    let voiceLLMServerScript: Script<ModelServerStarted>
    let voiceDownloadScript: Script<VoiceModelsDownloaded>
    let signInScript: Script<SignedIn>
    let profileReply: Result<ProfileQuestions, KeryxError>
    let profileResultReply: Result<ProfileResult, KeryxError>
    let migrationPlanReply: Result<MigrationPlan, KeryxError>
    let migrationScript: Script<MigrationDone>
    let projectQuestionReply: Result<ProjectContextQuestion, KeryxError>
    let projectDraftScript: Script<ProjectDraft>
    let projectResultReply: Result<ProjectContextResult, KeryxError>
    let homebrewPath: String?

    public init(
        status: Result<StatusReport, KeryxError> = .success(PreviewData.status),
        doctor: Result<DoctorReport, KeryxError> = .success(PreviewData.doctor),
        tasks: Result<[TaskItem], KeryxError> = .success(PreviewData.tasks.tasks),
        task: Result<TaskDetail, KeryxError> = .success(PreviewData.taskDetail),
        usage: Result<UsageReport, KeryxError> = .success(PreviewData.usage),
        models: Result<ModelsReport, KeryxError> = .success(PreviewData.models),
        config: Result<[ConfigRow], KeryxError> = .success(PreviewData.config),
        approvals: Result<ApprovalsReport, KeryxError> = .success(PreviewData.approvals),
        memory: Result<MemoryDocument, KeryxError> = .success(PreviewData.memory),
        voice: Result<VoiceStatus, KeryxError> = .success(PreviewData.voice),
        options: [String: Result<ConfigOptions, KeryxError>] = PreviewData.options.mapValues { .success($0) },
        server: Result<VoiceServerResult, KeryxError> = .success(PreviewClient.serverSaved),
        pull: Pull = .replay,
        setup: Setup = .replay,
        writeError: KeryxError? = nil,
        setupPlan: Result<SetupPlan, KeryxError> = .success(PreviewData.setupPlanPartway),
        pin: Result<PinStatus, KeryxError> = .success(PreviewData.pinFile),
        service: Result<ServiceReport, KeryxError> = .success(PreviewData.service),
        phoneNumbers: Result<PhoneNumbers, KeryxError> = .success(PreviewData.phoneNumbers),
        phoneWebhooks: Result<PhoneWebhooks, KeryxError> = .success(PreviewData.phoneWebhooks),
        phoneTunnel: Result<PhoneTunnel, KeryxError> = .success(PreviewData.phoneTunnel),
        integrations: Result<IntegrationsReport, KeryxError> = .success(PreviewData.integrations),
        plugins: Result<PluginsReport, KeryxError> = .success(PreviewData.plugins),
        tools: Result<ToolsReport, KeryxError> = .success(PreviewData.tools),
        sshHosts: Result<SshHostsReport, KeryxError> = .success(PreviewData.sshHosts),
        verify: Result<VerifyResult, KeryxError> = .success(PreviewClient.verified),
        modelServer: Script<ModelServerStarted> = .replay,
        voiceLLM: Script<VoiceLLMChosen> = .replay,
        voiceLLMServer: Script<ModelServerStarted> = .replay,
        voiceDownload: Script<VoiceModelsDownloaded> = .replay,
        signIn: Script<SignedIn> = .replay,
        profile: Result<ProfileQuestions, KeryxError> = .success(PreviewData.profileQuestions),
        profileResult: Result<ProfileResult, KeryxError> = .success(PreviewClient.profileWritten),
        migrationPlan: Result<MigrationPlan, KeryxError> = .success(PreviewData.migrationPlan),
        migration: Script<MigrationDone> = .replay,
        projectQuestion: Result<ProjectContextQuestion, KeryxError> = .success(PreviewData.projectContextQuestion),
        projectDraft: Script<ProjectDraft> = .replay,
        projectResult: Result<ProjectContextResult, KeryxError> = .success(PreviewClient.projectsKept),
        homebrew: String? = "/opt/homebrew/bin/brew"
    ) {
        statusReply = status
        doctorReply = doctor
        tasksReply = tasks
        taskReply = task
        usageReply = usage
        modelsReply = models
        configReply = config
        approvalsReply = approvals
        memoryReply = memory
        voiceReply = voice
        optionsReply = options
        serverReply = server
        pullBehaviour = pull
        setupBehaviour = setup
        self.writeError = writeError
        setupPlanReply = setupPlan
        pinReply = pin
        serviceReply = service
        phoneNumbersReply = phoneNumbers
        phoneWebhooksReply = phoneWebhooks
        phoneTunnelReply = phoneTunnel
        integrationsReply = integrations
        pluginsReply = plugins
        toolsReply = tools
        sshHostsReply = sshHosts
        verifyReply = verify
        modelServerScript = modelServer
        voiceLLMScript = voiceLLM
        voiceLLMServerScript = voiceLLMServer
        voiceDownloadScript = voiceDownload
        signInScript = signIn
        profileReply = profile
        profileResultReply = profileResult
        migrationPlanReply = migrationPlan
        migrationScript = migration
        projectQuestionReply = projectQuestion
        projectDraftScript = projectDraft
        projectResultReply = projectResult
        homebrewPath = homebrew
    }

    public func status() async throws -> StatusReport { try statusReply.get() }
    public func doctor() async throws -> DoctorReport { try doctorReply.get() }
    public func tasks(includeInternal: Bool, limit: Int) async throws -> [TaskItem] {
        try tasksReply.get().filter { includeInternal || !$0.internal }.prefix(limit).map { $0 }
    }
    public func task(id: Int) async throws -> TaskDetail { try taskReply.get() }
    public func usage(days: Int) async throws -> UsageReport { try usageReply.get() }
    public func models() async throws -> ModelsReport { try modelsReply.get() }
    public func config() async throws -> [ConfigRow] { try configReply.get() }
    public func approvals(limit: Int) async throws -> ApprovalsReport { try approvalsReply.get() }
    public func memory() async throws -> MemoryDocument { try memoryReply.get() }

    public func options(key: String) async throws -> ConfigOptions {
        try (optionsReply[key] ?? .success(PreviewData.builtinOptions(key))).get()
    }

    public func voice() async throws -> VoiceStatus { try voiceReply.get() }

    public func useVoiceOpenAI() async throws -> VoiceUseResult {
        try check()
        return VoiceUseResult(saved: ["VOICE_BASE_URL": .null], restartNeeded: true)
    }

    public func useVoiceServer(url: String, key: ServerKey, keepAnyway: Bool) async throws -> VoiceServerResult {
        try check()
        var answer = try serverReply.get()
        if keepAnyway, answer.saved == nil {
            answer.saved = ["VOICE_BASE_URL": .string(url)]
            answer.problem = nil
            answer.restartNeeded = true
        }
        return answer
    }

    /// A server that answered and was saved, with one thing worth knowing about it.
    public static let serverSaved = VoiceServerResult(
        saved: ["VOICE_BASE_URL": .string("http://studio.local:8000/v1"), "VOICE_API_KEY": .string("(set)")],
        problem: nil, restartNeeded: true,
        warnings: ["The server speaks over plain HTTP, so the call's audio crosses the network unencrypted"])

    /// A server that did not answer the handshake: nothing saved. Worded as the CLI words it.
    public static let serverProblem = VoiceServerResult(
        saved: nil, problem: "The voice server did not open a Realtime session.",
        detail: "could not open studio.local's Realtime socket (ConnectionRefusedError: [Errno 61] "
            + "Connect call failed ('studio.local', 8000))",
        restartNeeded: false, warnings: [])

    public func voiceHere(voice: String?) -> VoiceSetupStream {
        switch setupBehaviour {
        case .replay: Self.stream(PreviewData.voiceHereLines.compactMap(VoiceSetupEvent.parse), finishes: true)
        case let .holding(events): Self.stream(events, finishes: false)
        case let .events(events): Self.stream(events, finishes: true)
        case let .slowToStop(events):
            {
                let (stream, continuation) = AsyncThrowingStream<VoiceSetupEvent, Error>.makeStream()
                for event in events { continuation.yield(event) }
                return VoiceSetupStream(stream, stop: { _ = continuation })
            }()
        }
    }

    public func chooseVoiceLLM(_ choice: VoiceLLMChoice) -> VoiceLLMStream {
        Self.stream(voiceLLMScript, replay: PreviewData.voiceLLMHereLines)
    }

    /// `events`, then the end (`finishes`) or a wait until stopped — which, as the CLI does,
    /// says `cancelled` and ends.
    private static func stream(_ events: [VoiceSetupEvent], finishes: Bool) -> VoiceSetupStream {
        let (stream, continuation) = AsyncThrowingStream<VoiceSetupEvent, Error>.makeStream()
        for event in events { continuation.yield(event) }
        if finishes { continuation.finish() }
        return VoiceSetupStream(stream, stop: {
            continuation.yield(.cancelled)
            continuation.finish()
        })
    }

    public func pull(model: String) -> PullStream {
        let events: [PullEvent]
        var finishes = true
        switch pullBehaviour {
        case .replay:
            events = PreviewData.pullLines.compactMap(PullEvent.parse)
        case let .holding(done, total):
            events = [.progress(done: done, total: total)]
            finishes = false
        case let .events(given):
            events = given
        case let .slowToStop(done, total):
            let (stream, continuation) = AsyncThrowingStream<PullEvent, Error>.makeStream()
            continuation.yield(.progress(done: done, total: total))
            // The continuation is kept (and never finished), so the stream stays open.
            return PullStream(stream, stop: { _ = continuation })
        }
        let (stream, continuation) = AsyncThrowingStream<PullEvent, Error>.makeStream()
        for event in events { continuation.yield(event) }
        if finishes { continuation.finish() }
        // Stopped, it says what the CLI says, and ends.
        return PullStream(stream, stop: {
            continuation.yield(.cancelled)
            continuation.finish()
        })
    }

    public func use(model: String, defaultAgent: Bool) async throws -> UseResult {
        try check()
        let object: [String: Any] = [
            "saved": ["LOCAL_AGENT_MODEL": model],
            "llama_server": "/opt/homebrew/bin/llama-server",
            "restart_needed": true,
        ]
        let data = try JSONSerialization.data(withJSONObject: object)
        return try JSON.decoder.decode(UseResult.self, from: data)
    }

    public func set(key: String, value: String) async throws { try check() }
    public func setSecret(key: String, value: String) async throws { try check() }
    public func unset(key: String) async throws { try check() }
    public func setEscalation(enabled: Bool) async throws { try check() }
    public func restart(force: Bool) async throws { try check() }

    // MARK: Setup

    public func setupPlan() async throws -> SetupPlan { try setupPlanReply.get() }

    public func markSetupStep(_ step: String) async throws -> SetupMarkResult {
        try check()
        return SetupMarkResult(id: step, walked: true)
    }

    public func pin() async throws -> PinStatus { try pinReply.get() }

    public func setPin(_ pin: String, replace: Bool) async throws -> PinSetResult {
        try check()
        return PinSetResult(set: true, replaced: replace, restartNeeded: true)
    }

    public func verify(key: String, value: String) async throws -> VerifyResult { try verified(key) }
    public func verifyTwilio(accountSID: String, authToken: String) async throws -> VerifyResult {
        try verified("TWILIO_AUTH_TOKEN")
    }

    /// `verifyReply`, said of `key`.
    private func verified(_ key: String) throws -> VerifyResult {
        var answer = try verifyReply.get()
        answer.key = key
        return answer
    }

    /// A key that works (a Twilio pair names its account).
    public static let verified = VerifyResult(key: "OPENAI_API_KEY", ok: true, problem: nil, detail: nil)
    /// A key that does not, worded as the CLI words it.
    public static let notVerified = VerifyResult(
        key: "OPENAI_API_KEY", ok: false, problem: "OpenAI didn't accept the key (401).", detail: nil)

    // MARK: Setup sections

    public func profileQuestions() async throws -> ProfileQuestions { try profileReply.get() }

    public func previewProfile(_ answers: ProfileAnswers) async throws -> ProfileResult {
        var result = try profileResultReply.get()
        result.written = false
        result.walked = false
        if result.status == "written" { result.status = "unchanged" }
        return result
    }

    public func applyProfile(_ answers: ProfileAnswers) async throws -> ProfileResult {
        try check()
        return try profileResultReply.get()
    }

    public func migrationPlan() async throws -> MigrationPlan { try migrationPlanReply.get() }

    public func migrate() -> MigrationStream {
        Self.stream(migrationScript, replay: PreviewData.migrateLines)
    }

    public func projectContextQuestion() async throws -> ProjectContextQuestion { try projectQuestionReply.get() }

    public func draftProjectContext(_ scope: ProjectScope) -> ProjectDraftStream {
        Self.stream(projectDraftScript, replay: PreviewData.projectDraftLines)
    }

    public func applyProjectContext(_ keep: ProjectContextKeep) async throws -> ProjectContextResult {
        try check()
        return try projectResultReply.get()
    }

    /// The first memory written, as the CLI words it.
    public static let profileWritten = ProfileResult(
        status: "written", written: true,
        text: "# What Lyra knows about the owner\n\n## Standing facts\n\n- Works nights.\n",
        chars: 64, maxChars: 4000, problems: [], walked: true)

    /// One brief kept and a fact added.
    public static let projectsKept = ProjectContextResult(kept: ["keryx"], addedProjects: [], factsAdded: 1,
                                                          problems: [], walked: true)

    // MARK: Phone

    public func phoneNumbers() async throws -> PhoneNumbers { try phoneNumbersReply.get() }
    public func phoneWebhooks() async throws -> PhoneWebhooks { try phoneWebhooksReply.get() }
    public func phoneTunnel() async throws -> PhoneTunnel { try phoneTunnelReply.get() }

    public func homebrew() -> String? { homebrewPath }
    public func cloudflaredInstalled() -> Bool { (try? phoneTunnelReply.get().kind) != nil }

    /// Homebrew's first lines, then it holds until stopped.
    public func installCloudflared(brew: String) -> StoppableStream<String> {
        let (stream, continuation) = AsyncThrowingStream<String, Error>.makeStream()
        continuation.yield("==> Fetching cloudflared")
        continuation.yield("==> Downloading https://ghcr.io/v2/homebrew/core/cloudflared/manifests/2026.9.1")
        return StoppableStream(stream, stop: { continuation.finish() })
    }

    public func applyPhoneWebhooks() async throws -> WebhooksApplied {
        try check()
        return WebhooksApplied(applied: true, number: (try? phoneWebhooksReply.get().number) ?? "+15550001111")
    }

    // MARK: Service

    public func service() async throws -> ServiceReport { try serviceReply.get() }

    public func installService() async throws -> ServiceResult {
        try check()
        return ServiceResult(ok: true, message: "installed dev.keryx.agent, dev.keryx.tunnel")
    }

    public func uninstallService() async throws -> ServiceResult {
        try check()
        return ServiceResult(ok: true, message: "removed dev.keryx.agent, dev.keryx.tunnel")
    }

    public func stopService(all: Bool) async throws -> ServiceControlResult {
        try check()
        return ServiceControlResult(
            ok: true, message: all ? "Keryx and its model servers are stopped, and won't start at login until "
                + "they're started again." : "Keryx is stopped, and won't start at login until it's started again.",
            units: [ServiceControlUnit(kind: "agent", unit: "dev.keryx.agent", state: "stopped"),
                    ServiceControlUnit(kind: "tunnel", unit: "dev.keryx.tunnel", state: "stopped")])
    }

    public func startService(all: Bool) async throws -> ServiceControlResult {
        try check()
        return ServiceControlResult(
            ok: true, message: all ? "Keryx and its model servers are running." : "Keryx is running.",
            units: [ServiceControlUnit(kind: "agent", unit: "dev.keryx.agent", state: "running"),
                    ServiceControlUnit(kind: "tunnel", unit: "dev.keryx.tunnel", state: "running")])
    }

    public func startModelServer(runtime: String?) -> ModelServerStream {
        Self.stream(modelServerScript, replay: PreviewData.modelsStartLines)
    }

    public func stopModelServer() async throws -> ServiceResult {
        try check()
        return ServiceResult(ok: true, message: "removed dev.keryx.llm")
    }

    public func startVoiceLLMServer() -> ModelServerStream {
        Self.stream(voiceLLMServerScript, replay: PreviewData.voiceLLMServerStartLines)
    }

    public func stopVoiceLLMServer() async throws -> ServiceResult {
        try check()
        return ServiceResult(ok: true, message: "removed dev.keryx.voice-llm")
    }

    public func downloadVoiceModels() -> VoiceDownloadStream {
        Self.stream(voiceDownloadScript, replay: PreviewData.voiceDownloadLines)
    }

    // MARK: Accounts

    public func signIn(_ provider: SignInProvider, clientFile: String?) -> SignInStream {
        Self.stream(signInScript, replay: PreviewData.signInLines(provider))
    }

    public func signOut(_ provider: SignInProvider) async throws -> SignOutResult {
        try check()
        return SignOutResult(name: provider.rawValue, signedIn: false,
                             message: "Signed out: its stored sign-in was removed.")
    }

    public func integrations() async throws -> IntegrationsReport { try integrationsReply.get() }

    // MARK: Plugins

    public func plugins() async throws -> PluginsReport { try pluginsReply.get() }

    public func installPlugin(_ name: String, values: PluginValues) async throws -> PluginResult {
        try check()
        return PluginResult(name: name, on: true, message: "\(title(of: name)) is on from the next call.")
    }

    public func removePlugin(_ name: String) async throws -> PluginResult {
        try check()
        return PluginResult(name: name, on: false,
                            message: "\(title(of: name)) is off from the next call; its settings are kept.")
    }

    public func setPlugin(_ name: String, values: PluginValues) async throws -> PluginResult {
        try check()
        return PluginResult(name: name, on: true, message: "Settings saved.")
    }

    public func tools() async throws -> ToolsReport { try toolsReply.get() }
    public func sshHosts() async throws -> SshHostsReport { try sshHostsReply.get() }

    private func title(of plugin: String) -> String {
        (try? pluginsReply.get().plugin(plugin)?.title) ?? plugin
    }

    /// A `StreamEvent` stream as `script` says, `replay` being its fixture's lines. Stopped,
    /// it says `cancelled` and ends, as the CLI does — except `slowToStop`.
    static func stream<Done: StreamDone>(_ script: Script<Done>, replay lines: [String])
        -> StoppableStream<StreamEvent<Done>> {
        let (stream, continuation) = AsyncThrowingStream<StreamEvent<Done>, Error>.makeStream()
        let stop: @Sendable () -> Void = {
            continuation.yield(.cancelled)
            continuation.finish()
        }
        switch script {
        case .replay:
            lines.compactMap(StreamEvent<Done>.parse).forEach { continuation.yield($0) }
            continuation.finish()
        case let .events(events):
            events.forEach { continuation.yield($0) }
            continuation.finish()
        case let .holding(events):
            events.forEach { continuation.yield($0) }
        case let .slowToStop(events):
            events.forEach { continuation.yield($0) }
            // The continuation is kept (and never finished), so the stream stays open.
            return StoppableStream(stream, stop: { _ = continuation })
        case let .answering(first, then):
            first.forEach { continuation.yield($0) }
            let answered = Answered()
            let input = StreamInput(send: { _ in
                guard answered.once() else { return false }
                then.forEach { continuation.yield($0) }
                continuation.finish()
                return true
            }, close: {})
            return StoppableStream(stream, stop: stop, input: input)
        }
        return StoppableStream(stream, stop: stop)
    }

    private func check() throws {
        if let writeError { throw writeError }
    }
}

/// A `Locating` that answers at once, for previews: the fixture location unless told otherwise.
public struct PreviewLocator: Locating {
    public var result: Result<KeryxLocation, LocateFailure>

    public init(_ result: Result<KeryxLocation, LocateFailure> = .success(.fixture)) {
        self.result = result
    }

    public func locate(preference: String?) async -> Result<KeryxLocation, LocateFailure> {
        result
    }
}

/// True the first time only: a scripted stream answers one line.
private final class Answered: @unchecked Sendable {
    private let lock = NSLock()
    private var done = false

    func once() -> Bool {
        lock.withLock {
            defer { done = true }
            return !done
        }
    }
}
