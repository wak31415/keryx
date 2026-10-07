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
        case status, doctor, tasks, usage, models, config, approvals, memory, voice, all

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
    nonisolated static let doctorEvery = 10
    static let taskLimit = 200
    static let usageDays = 30
    static let approvalsLimit = 200

    public package(set) var location: Loadable<KeryxLocation> = .idle
    public package(set) var status: Loadable<StatusReport> = .idle
    public package(set) var doctor: Loadable<DoctorReport> = .idle
    public package(set) var tasks: Loadable<[TaskItem]> = .idle
    public package(set) var usage: Loadable<UsageReport> = .idle
    public package(set) var models: Loadable<ModelsReport> = .idle
    public package(set) var config: Loadable<[ConfigRow]> = .idle
    public package(set) var approvals: Loadable<ApprovalsReport> = .idle
    public package(set) var memory: Loadable<MemoryDocument> = .idle
    public package(set) var voice: Loadable<VoiceStatus> = .idle
    /// What each setting with options may be set to, by key (`loadOptions`).
    public package(set) var options: [String: Loadable<ConfigOptions>] = [:]
    public var taskDetail: [Int: Loadable<TaskDetail>] = [:]

    public var selection: SidebarItem = .overview
    public var selectedTaskID: Int?
    /// Whether the task list includes the tasks Keryx asked for itself (`--internal`).
    public var includeHousekeeping = false
    public var statusFilter: TaskStatus?
    /// A setting was written (or a model chosen) that the running service has yet to load.
    public var configNeedsRestart = false

    public let downloads: Downloads
    /// Setting up the voice server on this Mac.
    public let voiceSetup: VoiceSetup
    /// The client for the Keryx that was found; `nil` until one is.
    public package(set) var client: KeryxClient?

    public var menuState: MenuState {
        KeryxKit.menuState(location: location, status: status.value, statusError: status.error)
    }

    @ObservationIgnored private let locator: Locating
    @ObservationIgnored private let makeClient: @Sendable (KeryxLocation) -> KeryxClient
    @ObservationIgnored private let preferences: Preferences
    @ObservationIgnored private let timing: Timing
    /// Run with every poll: removes the Terminal scripts nobody ran (`TerminalLauncher`).
    @ObservationIgnored private let sweep: @Sendable () -> Void
    /// A sweep is under way (off the main actor); the next poll skips it rather than queue one.
    @ObservationIgnored private var sweeping = false
    @ObservationIgnored package var started = false
    @ObservationIgnored private var poll: Task<Void, Never>?
    /// How many menus show each key's options now, so a change reloads just those.
    @ObservationIgnored private var optionsShown: [String: Int] = [:]
    /// `config options` runs under way: at most `optionsAtOnce` (a slow provider each).
    @ObservationIgnored private var optionLoads = 0
    nonisolated static let optionsAtOnce = 3
    /// Bumped by every locate, so an answer from the Keryx found before is dropped.
    @ObservationIgnored private var generation = 0
    /// Polls since the poll last started.
    @ObservationIgnored private var pollTicks = 0

    /// `sweep` runs with every status poll, on a utility task of its own and never two at
    /// once; by default it removes the stale scripts
    /// `TerminalLauncher` left in the temporary folder while the app runs.
    public init(locator: Locating, makeClient: @escaping @Sendable (KeryxLocation) -> KeryxClient,
                preferences: Preferences, timing: Timing = .standard,
                sweep: @escaping @Sendable () -> Void = { TerminalLauncher.removeStaleScripts() }) {
        self.locator = locator
        self.makeClient = makeClient
        self.preferences = preferences
        self.timing = timing
        self.sweep = sweep
        downloads = Downloads()
        voiceSetup = VoiceSetup()
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
        options = [:]
        optionsShown = [:]
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
        pollTicks = 0
        poll = Task { [weak self] in
            while !Task.isCancelled {
                do { try await Task.sleep(for: interval) } catch { return }
                guard let self else { return }
                await self.pollTick()
            }
        }
    }

    /// One poll: the stale-script sweep started, then what `parts(forPoll:)` says for this tick.
    private func pollTick() async {
        pollTicks += 1
        startSweep()
        for part in Self.parts(forPoll: pollTicks) { await refresh(part) }
    }

    /// Runs `sweep` off the main actor (it lists a folder), unless one is still running.
    private func startSweep() {
        guard !sweeping else { return }
        sweeping = true
        let sweep = sweep
        Task.detached(priority: .utility) { [weak self] in
            sweep()
            await self?.sweepFinished()
        }
    }

    private func sweepFinished() {
        sweeping = false
    }

    /// What the `tick`-th poll (from 1) reads: the status, and on every `doctorEvery`-th
    /// the doctor too.
    nonisolated static func parts(forPoll tick: Int) -> [Part] {
        tick % doctorEvery == 0 ? [.status, .doctor] : [.status]
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
        case .voice: await load(\.voice, current) { try await client.voice() }
        }
    }

    /// Reads what `key` may be set to. A list already read is kept unless `force` (a menu
    /// appearing forces it, so a list is as fresh as the menu showing it); one being read
    /// is never asked for twice. At most `optionsAtOnce` are read at a time, the rest wait
    /// their turn. A list the CLI could not read live is a value (with its `error`), not a
    /// failure.
    public func loadOptions(_ key: String, force: Bool = false) async {
        guard let client else { return }
        let current = generation
        let known = options[key]
        if known?.isLoading == true || (!force && known?.value != nil) { return }
        options[key, default: .idle].begin()
        do {
            while optionLoads >= Self.optionsAtOnce { try await Task.sleep(for: .milliseconds(20)) }
            optionLoads += 1
            defer { optionLoads -= 1 }
            let list = try await client.options(key: key)
            guard current == generation else { return }
            options[key, default: .idle].succeed(list)
        } catch {
            guard current == generation else { return }
            settle(error) { options[key, default: .idle].fail($0) } stopped: {
                options[key, default: .idle].isLoading = false
            }
        }
    }

    /// A menu of `key`'s options appeared (`true`) or went (`false`).
    public func optionsMenu(_ key: String, shown: Bool) {
        let count = (optionsShown[key] ?? 0) + (shown ? 1 : -1)
        optionsShown[key] = count > 0 ? count : nil
    }

    /// Something the lists depend on changed (a key, a voice, a model): every list a menu
    /// shows now is read again, and the rest forgotten, to be read when next shown.
    func invalidateOptions() {
        let shown = Set(optionsShown.keys)
        for key in options.keys where !shown.contains(key) && options[key]?.isLoading != true {
            options[key] = nil
        }
        for key in shown {
            Task { await loadOptions(key, force: true) }
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
        case .voice: voice = .idle
        }
    }

    static func keryxError(_ error: Error) -> KeryxError {
        (error as? KeryxError) ?? .launch(error.localizedDescription)
    }

    // MARK: - Writing

    /// Sets a plain setting.
    public func set(_ row: ConfigRow, to value: String) async -> KeryxError? {
        await set(key: row.key, to: value)
    }

    /// Sets a plain setting by its key.
    public func set(key: String, to value: String) async -> KeryxError? {
        await writeSetting { try await $0.set(key: key, value: value) }
    }

    /// Chooses one of a setting's options (`config options`): an option whose value is
    /// empty ("Codex's default") means the setting unset, so it is unset — never set to "".
    public func choose(_ key: String, option value: String) async -> KeryxError? {
        value.isEmpty ? await unset(key) : await set(key: key, to: value)
    }

    /// Sets a secret; the value reaches the CLI on its stdin. A key can open a live list,
    /// so the lists are read again.
    public func setSecret(_ key: String, value: String) async -> KeryxError? {
        if let error = await writeSetting({ try await $0.setSecret(key: key, value: value) }) { return error }
        invalidateOptions()
        return nil
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
        invalidateOptions()
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
            self?.invalidateOptions()
            Task { await self?.refresh(.models) }
        }
    }

    // MARK: - Voice

    /// Speaks through OpenAI Realtime from the next restart.
    public func useVoiceOpenAI() async -> KeryxError? {
        var restartNeeded = false
        if let error = await perform({ restartNeeded = try await $0.useVoiceOpenAI().restartNeeded }) {
            return error
        }
        if restartNeeded { configNeedsRestart = true }
        invalidateOptions()
        await refresh([.voice, .config])
        return nil
    }

    /// Speaks through the voice server at `url` from the next restart. The answer says
    /// whether it was saved, and if not why (`problem`); a handshake that failed is an
    /// answer, not an error. A typed key reaches the CLI on its stdin; `.clear` removes the
    /// stored one.
    public func useVoiceServer(url: String, key: ServerKey, keepAnyway: Bool) async -> Result<VoiceServerResult, KeryxError> {
        var answer: VoiceServerResult?
        if let error = await perform({
            answer = try await $0.useVoiceServer(url: url, key: key, keepAnyway: keepAnyway)
        }) { return .failure(error) }
        guard let answer else { return .failure(.launch("Keryx said nothing.")) }
        if answer.saved != nil {
            if answer.restartNeeded { configNeedsRestart = true }
            invalidateOptions()
            await refresh([.voice, .config])
        }
        return .success(answer)
    }

    /// Sets up (or restarts) the voice server on this Mac, one setup at a time, and reads
    /// the voice again once it has worked. `voice`: a Kokoro voice to switch to.
    public func setUpVoiceHere(voice: String? = nil) {
        guard let client else { return }
        voiceSetup.start(voice: voice, client: client) { [weak self] in
            self?.configNeedsRestart = true
            self?.invalidateOptions()
            Task { await self?.refresh([.voice, .config, .models]) }
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
}
