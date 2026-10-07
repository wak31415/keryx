import Foundation
import KeryxKit

/// A `KeryxClient` that answers from `PreviewData`, for SwiftUI previews and snapshots.
/// Every document can be replaced (an empty list, a `KeryxError`, the migration status);
/// writes succeed unless `writeError` says otherwise; and a pull replays `pull.jsonl` or
/// holds at a progress value until it is cancelled.
public struct PreviewClient: KeryxClient {
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
        writeError: KeryxError? = nil
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

    /// A server that did not answer the handshake: nothing saved.
    public static let serverProblem = VoiceServerResult(
        saved: nil, problem: "studio.local:8000 did not answer the Realtime handshake: connection refused",
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

    public func installModelServer() async throws -> String {
        try check()
        return "Installed dev.keryx.llm and started it."
    }

    public func set(key: String, value: String) async throws { try check() }
    public func setSecret(key: String, value: String) async throws { try check() }
    public func unset(key: String) async throws { try check() }
    public func setEscalation(enabled: Bool) async throws { try check() }
    public func restart(force: Bool) async throws { try check() }

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
