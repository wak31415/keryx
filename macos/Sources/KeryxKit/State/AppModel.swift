import Foundation
import Observation

/// Everything the window and the menu bar show, and every change they ask Keryx for.
///
/// It finds Keryx, keeps one `Loadable` per document, and polls the status (and, more
/// rarely, the doctor) while the app runs. Errors are stored where the document is, never
/// thrown; a write returns its error for the view that asked to show beside the control.
@Observable @MainActor
public final class AppModel {
    /// What `refresh` reloads.
    public enum Part: Sendable, CaseIterable {
        case status, doctor, tasks, usage, models, config, approvals, memory, all

        static let documents: [Part] = allCases.filter { $0 != .all }
    }

    /// How long the model waits: between status polls (`nil`: no polling), and before it
    /// looks again after starting the model server or a restart.
    public struct Timing: Sendable {
        public var poll: Duration?
        public var afterModelServer: Duration
        public var afterRestart: Duration

        public init(poll: Duration? = .seconds(30), afterModelServer: Duration = .seconds(2),
                    afterRestart: Duration = .seconds(3)) {
            self.poll = poll
            self.afterModelServer = afterModelServer
            self.afterRestart = afterRestart
        }

        public static let standard = Timing()
    }

    /// The doctor is run on every this-many-th poll.
    static let doctorEvery = 10
    static let taskLimit = 200
    static let usageDays = 30
    static let approvalsLimit = 200

    public private(set) var location: Loadable<KeryxLocation> = .idle
    public private(set) var status: Loadable<StatusReport> = .idle
    public private(set) var doctor: Loadable<DoctorReport> = .idle
    public private(set) var tasks: Loadable<[TaskItem]> = .idle
    public private(set) var usage: Loadable<UsageReport> = .idle
    public private(set) var models: Loadable<ModelsReport> = .idle
    public private(set) var config: Loadable<[ConfigRow]> = .idle
    public private(set) var approvals: Loadable<ApprovalsReport> = .idle
    public private(set) var memory: Loadable<MemoryDocument> = .idle
    public var taskDetail: [Int: Loadable<TaskDetail>] = [:]

    public var selection: SidebarItem = .overview
    public var selectedTaskID: Int?
    /// Whether the task list includes the tasks Keryx asked for itself (`--internal`).
    public var includeHousekeeping = false
    public var statusFilter: TaskStatus?
    /// A setting was written (or a model chosen) that the running service has yet to load.
    public var configNeedsRestart = false

    public let downloads: Downloads
    /// The client for the Keryx that was found; `nil` until one is.
    public private(set) var client: KeryxClient?

    public var menuState: MenuState {
        KeryxKit.menuState(location: location, status: status.value, statusError: status.error)
    }

    @ObservationIgnored private let locator: Locating
    @ObservationIgnored private let makeClient: @Sendable (KeryxLocation) -> KeryxClient
    @ObservationIgnored private let preferences: Preferences
    @ObservationIgnored private let timing: Timing
    @ObservationIgnored private var started = false
    @ObservationIgnored private var poll: Task<Void, Never>?
    /// Bumped by every locate, so an answer from the Keryx found before is dropped.
    @ObservationIgnored private var generation = 0

    public init(locator: Locating, makeClient: @escaping @Sendable (KeryxLocation) -> KeryxClient,
                preferences: Preferences, timing: Timing = .standard) {
        self.locator = locator
        self.makeClient = makeClient
        self.preferences = preferences
        self.timing = timing
        downloads = Downloads()
    }

    // MARK: - Finding Keryx

    /// Finds Keryx, loads its status and doctor, and starts the status poll. Once only; a
    /// second call while started does nothing.
    public func start() async {
        guard !started else { return }
        started = true
        startPolling()
        await locate()
    }

    /// Stops the status poll; a later `start` finds Keryx again.
    public func stop() {
        poll?.cancel()
        poll = nil
        started = false
    }

    /// Forgets the Keryx found before and everything read from it, and looks again — after
    /// the Keryx folder preference changes.
    public func relocate() async {
        client = nil
        location = .idle
        for part in Part.documents { reset(part) }
        taskDetail = [:]
        configNeedsRestart = false
        if !started {
            started = true
            startPolling()
        }
        await locate()
    }

    private func locate() async {
        generation += 1
        let current = generation
        location.begin()
        let result = await locator.locate(preference: preferences.keryxFolder)
        guard current == generation else { return }
        switch result {
        case let .success(found):
            location = Loadable(value: found, updatedAt: Date())
            client = makeClient(found)
            await refresh([.status, .doctor])
        case let .failure(failure):
            location = Loadable(error: .notFound(failure))
            client = nil
        }
    }

    private func startPolling() {
        guard poll == nil, let interval = timing.poll else { return }
        poll = Task { [weak self] in
            var iteration = 0
            while !Task.isCancelled {
                do { try await Task.sleep(for: interval) } catch { return }
                guard let self else { return }
                iteration += 1
                await self.refresh(.status)
                if iteration % Self.doctorEvery == 0 { await self.refresh(.doctor) }
            }
        }
    }

    // MARK: - Reading

    /// Reloads `part` (or every document). Does nothing until Keryx is found.
    public func refresh(_ part: Part) async {
        guard let client else { return }
        let current = generation
        switch part {
        case .all: await refresh(Part.documents)
        case .status: await load(\.status, current) { try await client.status() }
        case .doctor: await load(\.doctor, current) { try await client.doctor() }
        case .tasks:
            let includeInternal = includeHousekeeping
            await load(\.tasks, current) {
                try await client.tasks(includeInternal: includeInternal, limit: Self.taskLimit)
            }
        case .usage: await load(\.usage, current) { try await client.usage(days: Self.usageDays) }
        case .models: await load(\.models, current) { try await client.models() }
        case .config: await load(\.config, current) { try await client.config() }
        case .approvals:
            await load(\.approvals, current) { try await client.approvals(limit: Self.approvalsLimit) }
        case .memory: await load(\.memory, current) { try await client.memory() }
        }
    }

    /// Loads one task's detail into `taskDetail[id]`.
    public func loadTask(_ id: Int) async {
        guard let client else { return }
        let current = generation
        taskDetail[id, default: .idle].begin()
        do {
            let detail = try await client.task(id: id)
            guard current == generation else { return }
            taskDetail[id, default: .idle].succeed(detail)
        } catch {
            guard current == generation else { return }
            settle(error) { taskDetail[id, default: .idle].fail($0) } stopped: {
                taskDetail[id, default: .idle].isLoading = false
            }
        }
    }

    private func refresh(_ parts: [Part]) async {
        await withTaskGroup(of: Void.self) { group in
            for part in parts {
                group.addTask { await self.refresh(part) }
            }
        }
    }

    private func load<Value: Sendable>(
        _ path: ReferenceWritableKeyPath<AppModel, Loadable<Value>>, _ current: Int,
        _ fetch: @Sendable () async throws -> Value
    ) async {
        self[keyPath: path].begin()
        do {
            let value = try await fetch()
            guard current == generation else { return }
            self[keyPath: path].succeed(value)
        } catch {
            guard current == generation else { return }
            settle(error) { self[keyPath: path].fail($0) } stopped: {
                self[keyPath: path].isLoading = false
            }
        }
    }

    /// A cancelled load records nothing; anything else is stored as a `KeryxError`.
    private func settle(_ error: Error, failed: (KeryxError) -> Void, stopped: () -> Void) {
        if error is CancellationError { stopped() } else { failed(Self.keryxError(error)) }
    }

    private func reset(_ part: Part) {
        switch part {
        case .all: break
        case .status: status = .idle
        case .doctor: doctor = .idle
        case .tasks: tasks = .idle
        case .usage: usage = .idle
        case .models: models = .idle
        case .config: config = .idle
        case .approvals: approvals = .idle
        case .memory: memory = .idle
        }
    }

    static func keryxError(_ error: Error) -> KeryxError {
        (error as? KeryxError) ?? .launch(error.localizedDescription)
    }

    // MARK: - Writing

    /// Sets a plain setting.
    public func set(_ row: ConfigRow, to value: String) async -> KeryxError? {
        await writeSetting { try await $0.set(key: row.key, value: value) }
    }

    /// Sets a secret; the value reaches the CLI on its stdin.
    public func setSecret(_ key: String, value: String) async -> KeryxError? {
        await writeSetting { try await $0.setSecret(key: key, value: value) }
    }

    /// Removes a setting from the store, so its default (or the environment) applies.
    public func unset(_ key: String) async -> KeryxError? {
        await writeSetting { try await $0.unset(key: key) }
    }

    /// Turns the approval calls on or off (the kill switch; no restart needed).
    public func setEscalation(enabled: Bool) async -> KeryxError? {
        if let error = await perform({ try await $0.setEscalation(enabled: enabled) }) { return error }
        await refresh([.approvals, .status])
        return nil
    }

    /// Restarts the service, then reads its status again a few seconds later. A refusal
    /// because calls are in progress comes back as `.liveCalls`.
    public func restart(force: Bool) async -> KeryxError? {
        if let error = await perform({ try await $0.restart(force: force) }) { return error }
        configNeedsRestart = false
        try? await Task.sleep(for: timing.afterRestart)
        await refresh(.status)
        return nil
    }

    /// Chooses a downloaded model for the local agent.
    public func use(model: String, defaultAgent: Bool) async -> KeryxError? {
        var restartNeeded = false
        if let error = await perform({
            restartNeeded = try await $0.use(model: model, defaultAgent: defaultAgent).restartNeeded
        }) { return error }
        if restartNeeded { configNeedsRestart = true }
        await refresh([.models, .config])
        return nil
    }

    /// Installs (or restarts) the model server, then reads the models again once it has had
    /// a moment to come up.
    public func startModelServer() async -> KeryxError? {
        if let error = await perform({ _ = try await $0.installModelServer() }) { return error }
        try? await Task.sleep(for: timing.afterModelServer)
        await refresh(.models)
        return nil
    }

    /// Downloads `model` (one at a time), and reads the models again when it is done.
    public func download(_ model: String) {
        guard let client else { return }
        downloads.start(model, client: client) { [weak self] in
            Task { await self?.refresh(.models) }
        }
    }

    private func writeSetting(_ write: (KeryxClient) async throws -> Void) async -> KeryxError? {
        if let error = await perform(write) { return error }
        configNeedsRestart = true
        await refresh(.config)
        return nil
    }

    private func perform(_ action: (KeryxClient) async throws -> Void) async -> KeryxError? {
        guard let client else {
            return location.error ?? .notFound(LocateFailure(lookedAt: []))
        }
        do {
            try await action(client)
            return nil
        } catch {
            return Self.keryxError(error)
        }
    }

    // MARK: - Previews

    /// A model already holding what `client` answers, for previews and snapshots: no
    /// polling, nothing awaited. With a `.failure` location it is the not-found state.
    public static func preview(
        client: PreviewClient = PreviewClient(),
        location: Result<KeryxLocation, LocateFailure> = .success(.fixture)
    ) -> AppModel {
        let model = AppModel(locator: PreviewLocator(location), makeClient: { _ in client },
                             preferences: Preferences(), timing: Timing(poll: nil))
        model.started = true
        switch location {
        case let .failure(failure):
            model.location = Loadable(error: .notFound(failure))
            return model
        case let .success(found):
            model.location = Loadable(value: found, updatedAt: Date())
        }
        model.client = client
        model.status = .settled(client.statusReply)
        model.doctor = .settled(client.doctorReply)
        model.tasks = .settled(client.tasksReply.map { $0.filter { !$0.internal } })
        model.usage = .settled(client.usageReply)
        model.models = .settled(client.modelsReply)
        model.config = .settled(client.configReply)
        model.approvals = .settled(client.approvalsReply)
        model.memory = .settled(client.memoryReply)
        if case let .success(detail) = client.taskReply {
            model.taskDetail[detail.task.id] = .settled(.success(detail))
        }
        return model
    }
}

extension Loadable {
    /// Loaded, or failed, now.
    static func settled(_ result: Result<Value, KeryxError>) -> Self {
        switch result {
        case let .success(value): Loadable(value: value, updatedAt: Date())
        case let .failure(error): Loadable(error: error)
        }
    }
}
