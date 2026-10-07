import Foundation

/// A `KeryxClient` that answers from `PreviewData`, for SwiftUI previews and snapshots.
/// Every document can be replaced (an empty list, a `KeryxError`, the migration status);
/// writes succeed unless `writeError` says otherwise; and a pull replays `pull.jsonl` or
/// holds at a progress value until it is cancelled.
public struct PreviewClient: KeryxClient {
    /// What `pull(model:)` does.
    public enum Pull: Sendable {
        /// Yields the events of `PreviewData.pullLines`, then finishes.
        case replay
        /// Yields one progress event, then waits until the download is cancelled.
        case holding(done: Int64, total: Int64)
        /// Yields these events, then finishes.
        case events([PullEvent])
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
    let pullBehaviour: Pull
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
        pull: Pull = .replay,
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
        pullBehaviour = pull
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

    public func pull(model: String) -> AsyncThrowingStream<PullEvent, Error> {
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
        }
        return AsyncThrowingStream { continuation in
            for event in events { continuation.yield(event) }
            if finishes { continuation.finish() }
        }
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
