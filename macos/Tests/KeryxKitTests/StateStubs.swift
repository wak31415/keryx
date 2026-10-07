import Foundation
@testable import KeryxKit
import KeryxPreview

/// A `KeryxClient` whose every answer is set by the test, and which records what it was
/// asked, in order, as the CLI subcommand it stands for.
final class StubClient: KeryxClient, @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [String] = []
    private var replies = Replies()

    struct Replies {
        var status: Result<StatusReport, KeryxError> = .success(PreviewData.status)
        var doctor: Result<DoctorReport, KeryxError> = .success(PreviewData.doctor)
        var tasks: Result<[TaskItem], KeryxError> = .success(PreviewData.tasks.tasks)
        var task: Result<TaskDetail, KeryxError> = .success(PreviewData.taskDetail)
        var usage: Result<UsageReport, KeryxError> = .success(PreviewData.usage)
        var models: Result<ModelsReport, KeryxError> = .success(PreviewData.models)
        var config: Result<[ConfigRow], KeryxError> = .success(PreviewData.config)
        var approvals: Result<ApprovalsReport, KeryxError> = .success(PreviewData.approvals)
        var memory: Result<MemoryDocument, KeryxError> = .success(PreviewData.memory)
        var use: Result<UseResult, KeryxError> = .success(StubClient.useResult(restartNeeded: true))
        var write: KeryxError?
        var pull: @Sendable (String) -> PullStream = { _ in
            PullStream(AsyncThrowingStream { $0.finish() })
        }
    }

    init(_ configure: (inout Replies) -> Void = { _ in }) {
        configure(&replies)
    }

    var calls: [String] { lock.withLock { recorded } }

    func update(_ change: (inout Replies) -> Void) {
        lock.withLock { change(&replies) }
    }

    static func useResult(restartNeeded: Bool) -> UseResult {
        let json = #"{"saved": {"LOCAL_AGENT_MODEL": "m"}, "llama_server": null, "restart_needed": \#(restartNeeded)}"#
        return try! JSON.decoder.decode(UseResult.self, from: Data(json.utf8))
    }

    private func answer<T>(_ call: String, _ reply: (Replies) -> Result<T, KeryxError>) throws -> T {
        try lock.withLock {
            recorded.append(call)
            return try reply(replies).get()
        }
    }

    private func write(_ call: String) throws {
        try lock.withLock {
            recorded.append(call)
            if let error = replies.write { throw error }
        }
    }

    func status() async throws -> StatusReport { try answer("status") { $0.status } }
    func doctor() async throws -> DoctorReport { try answer("doctor") { $0.doctor } }
    func tasks(includeInternal: Bool, limit: Int) async throws -> [TaskItem] {
        try answer("tasks list\(includeInternal ? " --internal" : "")") { $0.tasks }
    }
    func task(id: Int) async throws -> TaskDetail { try answer("tasks show \(id)") { $0.task } }
    func usage(days: Int) async throws -> UsageReport { try answer("tasks usage") { $0.usage } }
    func models() async throws -> ModelsReport { try answer("models list") { $0.models } }
    func pull(model: String) -> PullStream {
        let make = lock.withLock {
            recorded.append("models pull \(model)")
            return replies.pull
        }
        return make(model)
    }
    func use(model: String, defaultAgent: Bool) async throws -> UseResult {
        try answer("models use \(model)\(defaultAgent ? " --default-agent" : "")") { $0.use }
    }
    func installModelServer() async throws -> String {
        try write("install-launchd.sh --llm")
        return "installed"
    }
    func config() async throws -> [ConfigRow] { try answer("config list") { $0.config } }
    func set(key: String, value: String) async throws { try write("config set \(key) \(value)") }
    func setSecret(key: String, value: String) async throws { try write("config set \(key) --stdin") }
    func unset(key: String) async throws { try write("config unset \(key)") }
    func approvals(limit: Int) async throws -> ApprovalsReport { try answer("approvals") { $0.approvals } }
    func setEscalation(enabled: Bool) async throws {
        try write("approvals \(enabled ? "--enable" : "--disable")")
    }
    func memory() async throws -> MemoryDocument { try answer("memory") { $0.memory } }
    func restart(force: Bool) async throws { try write("restart\(force ? " --force" : "")") }
}

/// A `Locating` that answers what the test says, and remembers the preference it was given.
final class StubLocator: Locating, @unchecked Sendable {
    private let lock = NSLock()
    private var result: Result<KeryxLocation, LocateFailure>
    private var preferences: [String?] = []

    init(_ result: Result<KeryxLocation, LocateFailure> = .success(.fixture)) {
        self.result = result
    }

    var askedWith: [String?] { lock.withLock { preferences } }

    func answer(_ result: Result<KeryxLocation, LocateFailure>) {
        lock.withLock { self.result = result }
    }

    func locate(preference: String?) async -> Result<KeryxLocation, LocateFailure> {
        lock.withLock {
            preferences.append(preference)
            return result
        }
    }
}

/// A pull stream the test drives by hand: it yields what the test sends and finishes when
/// the test says, and it records whether it was asked to stop (which, for the real client,
/// signals the `keryx models pull` child's group) and whether its consumer went away.
final class ManualPull: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: AsyncThrowingStream<PullEvent, Error>.Continuation?
    private var terminated = false
    private var stops = 0

    var wasTerminated: Bool { lock.withLock { terminated } }
    var stopRequests: Int { lock.withLock { stops } }

    func stream() -> PullStream {
        let events = AsyncThrowingStream<PullEvent, Error> { continuation in
            continuation.onTermination = { [weak self] _ in
                guard let self else { return }
                self.lock.withLock { self.terminated = true }
            }
            lock.withLock { self.continuation = continuation }
        }
        return PullStream(events, stop: { [weak self] in
            guard let self else { return }
            self.lock.withLock { self.stops += 1 }
        })
    }

    func send(_ event: PullEvent) { lock.withLock { continuation }?.yield(event) }
    func finish(throwing error: Error? = nil) { lock.withLock { continuation }?.finish(throwing: error) }
}

/// Waits for something the model does in the background (the status poll, a download's
/// progress). It returns as soon as `condition` holds; the generous timeout is for a main
/// actor kept busy by the suites that render views, never a wait in the passing case.
@MainActor
func eventually(timeout: Duration = .seconds(30), _ condition: @MainActor () -> Bool) async -> Bool {
    let clock = ContinuousClock()
    let deadline = clock.now + timeout
    while clock.now < deadline {
        if condition() { return true }
        try? await Task.sleep(for: .milliseconds(2))
    }
    return condition()
}

func freshDefaults() -> UserDefaults {
    let name = "keryx-tests-\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: name)!
    defaults.removePersistentDomain(forName: name)
    return defaults
}
