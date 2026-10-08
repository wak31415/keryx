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
        case status, doctor, tasks, usage, models, config, approvals, memory, voice
        case setupPlan, pin, service, phoneNumbers, phoneWebhooks, phoneTunnel
        case integrations, plugins, tools, sshHosts
        case all

        static let documents: [Part] = allCases.filter { $0 != .all }

        /// What `.all` leaves out, each read by the screen that shows it: the reads that ask
        /// Twilio (the account's numbers, where the number's webhooks point) and the tunnel,
        /// which asks `PUBLIC_HOST` — the Phone section's (`refreshPhone`) — and the ssh hosts,
        /// Plugins'. So a refresh elsewhere never reaches outside this Mac for them.
        public static let onlyWhenShown: Set<Part> = [.phoneNumbers, .phoneWebhooks, .phoneTunnel, .sshHosts]

        /// What `onlyWhenShown` reads, while `item` shows: the section's own.
        static func shown(with item: SidebarItem) -> [Part] {
            switch item {
            case .phone: [.phoneNumbers, .phoneWebhooks, .phoneTunnel]
            case .plugins: [.sshHosts]
            default: []
            }
        }

        /// What `.all` reloads: every document but `onlyWhenShown`.
        static let everyday: [Part] = documents.filter { !onlyWhenShown.contains($0) }
    }

    /// How long the model waits: between status polls (`nil`: no polling), and before it
    /// looks again after a restart.
    public struct Timing: Sendable {
        public var poll: Duration?
        public var afterRestart: Duration

        public init(poll: Duration? = .seconds(30), afterRestart: Duration = .seconds(3)) {
            self.poll = poll
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
    /// The steps to a first working call (`keryx setup plan`).
    public package(set) var setupPlan: Loadable<SetupPlan> = .idle
    public package(set) var pin: Loadable<PinStatus> = .idle
    /// Keryx's units (`keryx service`).
    public package(set) var service: Loadable<ServiceReport> = .idle
    /// The Twilio account's numbers. Read only when asked for (`.phoneNumbers`, never by
    /// `.all`): it asks Twilio.
    public package(set) var phoneNumbers: Loadable<PhoneNumbers> = .idle
    /// Where `TWILIO_NUMBER`'s webhooks point; asks Twilio too, so `.all` leaves it out.
    public package(set) var phoneWebhooks: Loadable<PhoneWebhooks> = .idle
    public package(set) var phoneTunnel: Loadable<PhoneTunnel> = .idle
    public package(set) var integrations: Loadable<IntegrationsReport> = .idle
    public package(set) var plugins: Loadable<PluginsReport> = .idle
    /// The owner's own voice tools (`keryx tools`).
    public package(set) var tools: Loadable<ToolsReport> = .idle
    /// The ssh hosts `cluster_stats` could ask (`keryx plugins hosts`).
    public package(set) var sshHosts: Loadable<SshHostsReport> = .idle
    /// What each setting with options may be set to, by key (`loadOptions`).
    public package(set) var options: [String: Loadable<ConfigOptions>] = [:]
    public var taskDetail: [Int: Loadable<TaskDetail>] = [:]

    public var selection: SidebarItem = .overview {
        // Choosing another section drops a request nobody there answered, so it cannot
        // spring open later.
        didSet { if let requested, requested.destination != selection { self.requested = nil } }
    }
    /// What another window (the Setup Assistant) asked the main window to do, until the
    /// screen that does it says so (`request(_:)`, `didHandle(_:)`) or another section is
    /// chosen.
    public private(set) var requested: AppRequest?
    /// What Local Models should scroll to and mark when it next shows: a catalog model's
    /// name, or one of its sections. Local Models clears it once shown.
    public var modelsSpotlight: String?
    public var selectedTaskID: Int?
    /// Whether the task list includes the tasks Keryx asked for itself (`--internal`).
    public var includeHousekeeping = false
    public var statusFilter: TaskStatus?
    /// A setting was written (or a model chosen) that the running service has yet to load.
    public var configNeedsRestart = false
    /// Keryx's own service is being installed or uninstalled (`installService`,
    /// `uninstallService()`), which quitting asks about.
    public private(set) var serviceChange: ServiceChange?
    /// Whether this model keeps Keryx answering calls while the app runs: it starts Keryx's
    /// unit once setup has reached it (`ensureAnswering`), and stops it when the app quits
    /// (`stopAnswering`). The app sets it at launch — never for a sandbox run
    /// (`KERYX_APP_ENVIRONMENT=inherit`), whose `keryx` would install the real launch agents.
    public var keepsAnswering = false
    /// Why the last start failed — the automatic one, or Start in the Overview — until one
    /// works.
    public private(set) var startFailure: KeryxError?
    /// A start has been tried for the Keryx found now, the automatic one or Start in the
    /// Overview: the automatic one is tried once a launch, and never after Start, so a start
    /// that fails, or a unit that will not stay up, is not tried again and again.
    @ObservationIgnored private var startTried = false
    /// The check `ensureAnswering` runs, while it runs.
    @ObservationIgnored private var ensuring: Task<Void, Never>?
    /// The app is quitting (`prepareToQuit`): nothing starts Keryx from now on.
    @ObservationIgnored private var quitting = false
    /// The install or uninstall under way, held so quitting can stop it (`cancelRuns`):
    /// cancelled, its runner stops the installer's whole process group.
    @ObservationIgnored private var serviceTask: Task<Result<ServiceResult, KeryxError>, Never>?
    /// The model server is being stopped (`stopModelServer`): nothing starts it meanwhile.
    public private(set) var stoppingModelServer = false
    /// The voice LLM's own server is being stopped (`stopVoiceLLMServer`).
    public private(set) var stoppingVoiceLLMServer = false
    /// The short writes under way — a setting, the PIN, the webhooks … — which quitting gives
    /// a moment to finish, and names if they have not.
    public private(set) var writes: [UUID: Write] = [:]

    /// A setting's save that failed after its own row had already gone — a Configuration
    /// tab switch, a sidebar section, or a programmatic `selection` change tears a row down
    /// as it commits what was typed, before the write answers, so there is no row left to
    /// show the error on. Shown as a banner over the whole window instead; replaced by the
    /// next one, and `nil` once there is none or it is dismissed.
    public package(set) var settingSaveFailure: StreamFailure?

    /// Records `title`'s save as failed with `error`, for the window-level banner.
    package func reportSettingSaveFailure(title: String, error: KeryxError) {
        reportSettingSaveFailure(title: title, sentence: error.message, details: error.details)
    }

    /// Records `title`'s save as failed with a local sentence (a number field's text that
    /// is not one), rather than a `KeryxError` the CLI refused.
    package func reportSettingSaveFailure(title: String, sentence: String, details: String? = nil) {
        settingSaveFailure = StreamFailure("Couldn't save \(title): \(sentence)", detail: details)
    }

    /// Dismisses the save-failure banner.
    public func dismissSettingSaveFailure() {
        settingSaveFailure = nil
    }

    /// A short write to Keryx, as quitting names it.
    public enum Write: Sendable, Equatable, Hashable, CaseIterable {
        case setting, pin, webhooks, voice, model, setupStep, plugin, signOut, modelServerStop, restart
        case memory, projects, voiceLLMServerStop

        /// "the phone PIN is being saved": a clause for the quit question.
        public var clause: String {
            switch self {
            case .setting: "a setting is being saved"
            case .pin: "the phone PIN is being saved"
            case .webhooks: "the phone number's webhooks are being changed"
            case .voice: "the voice is being changed"
            case .model: "a model is being chosen"
            case .setupStep: "a setup step is being recorded"
            case .plugin: "a plugin is being changed"
            case .signOut: "an account is being signed out"
            case .modelServerStop: "the model server is being stopped"
            case .restart: "Keryx is being restarted"
            case .memory: "what the assistant knows about you is being saved"
            case .projects: "the project briefs are being saved"
            case .voiceLLMServerStop: "the voice LLM's server is being stopped"
            }
        }
    }

    /// What `writes` holds, each kind once, in the order `Write` lists them.
    public var writing: [Write] {
        let kinds = Set(writes.values)
        return Write.allCases.filter(kinds.contains)
    }

    /// Waits until no write is under way, or `limit` has passed: true once there is none.
    public func writesFinish(within limit: Duration) async -> Bool {
        let deadline = ContinuousClock.now + limit
        while !writes.isEmpty, ContinuousClock.now < deadline {
            try? await Task.sleep(for: .milliseconds(50))
        }
        return writes.isEmpty
    }

    /// What is being done to Keryx's own service.
    public enum ServiceChange: Sendable, Equatable {
        case installing, uninstalling
    }

    public let downloads: Downloads
    /// Setting up the voice server on this Mac.
    public let voiceSetup: VoiceSetup
    /// The latest Set Up… on This Mac, while it may still be showing: whether it began by
    /// starting the model server. `nil` before one.
    public private(set) var thisMacSetup: ThisMacSetup?
    /// The This Mac setup whose model server, once it serves, goes on to the voice; cleared
    /// when it is stopped, so a `done` that beats the stop starts nothing. Observed: while
    /// it is set, the speech models may not be downloaded (`canDownloadVoiceModels`).
    private var chain: UUID?
    /// Starting the model server here (`keryx models start`).
    public let modelServerRun: StreamRun<ModelServerStarted>
    /// Choosing where a voice on this Mac takes its words from (`keryx voice llm`).
    public let voiceLLMRun: StreamRun<VoiceLLMChosen>
    /// What `voiceLLMRun` was last asked to choose, for its steps and Try Again — a typed
    /// key never kept: it is `.stored` here once it has been sent.
    public private(set) var voiceLLMChoice: VoiceLLMChoice?
    /// Starting the voice LLM's own server here (`keryx models start --voice-llm`).
    public let voiceLLMServerRun: StreamRun<ModelServerStarted>
    /// Downloading the voice's speech models (`keryx voice download`).
    public let voiceDownloadRun: StreamRun<VoiceModelsDownloaded>
    /// A sign-in (`keryx auth login NAME`), one at a time whichever it is.
    public let signInRun: StreamRun<SignedIn>
    /// Moving Jarvis's files to Keryx's (`keryx migrate --yes`).
    public let migrationRun: StreamRun<MigrationDone>
    /// An agent reading the projects for their briefs (`keryx setup project-context`).
    public let projectDraftRun: StreamRun<ProjectDraft>
    /// SEAM: a coding agent's test task (`keryx auth smoke NAME`), once a client can run it.
    /// Held here, not by a screen, so going back and forth never starts a second (paid) run,
    /// and quitting stops it.
    public let smokeRun: StreamRun<AgentSmoked>
    /// Cloudflare Tunnel installed with Homebrew, after a click.
    public let tunnelInstall: ProgramInstall
    /// How the setup wizard opens next, or opened last: first-run or review, with a fresh id
    /// each time it is asked to open, so a window already open starts again at Welcome.
    public private(set) var wizardOpening = WizardOpening(mode: .firstRun)
    /// The provider of the sign-in `signInRun` holds — running, stopping, failed or
    /// finished — and `nil` once it holds none (cancelled, or dismissed).
    public var signingIn: SignInProvider? { signInRun.phase == nil ? nil : lastSignIn }
    @ObservationIgnored private var lastSignIn: SignInProvider?
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
        modelServerRun = StreamRun()
        voiceLLMRun = StreamRun()
        voiceLLMServerRun = StreamRun()
        voiceDownloadRun = StreamRun()
        signInRun = StreamRun()
        migrationRun = StreamRun()
        projectDraftRun = StreamRun()
        smokeRun = StreamRun()
        tunnelInstall = ProgramInstall()
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
        startFailure = nil
        startTried = false
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
            if keepsAnswering { checkAnswering() }
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
        case .all: await refresh(Part.everyday)
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
        case .setupPlan:
            await load(\.setupPlan, current) { try await client.setupPlan() }
            // A plan that now lets Keryx start (the phone just set up) starts it.
            if keepsAnswering, ensuring == nil, answeringStep == .start { checkAnswering() }
        case .pin: await load(\.pin, current) { try await client.pin() }
        case .service: await load(\.service, current) { try await client.service() }
        case .phoneNumbers: await load(\.phoneNumbers, current) { try await client.phoneNumbers() }
        case .phoneWebhooks: await load(\.phoneWebhooks, current) { try await client.phoneWebhooks() }
        case .phoneTunnel: await load(\.phoneTunnel, current) { try await client.phoneTunnel() }
        case .integrations: await load(\.integrations, current) { try await client.integrations() }
        case .plugins: await load(\.plugins, current) { try await client.plugins() }
        case .tools: await load(\.tools, current) { try await client.tools() }
        case .sshHosts: await load(\.sshHosts, current) { try await client.sshHosts() }
        }
    }

    /// Reloads each of `parts`, side by side.
    public func refresh(_ parts: Part...) async {
        await refresh(parts)
    }

    /// What Refresh (the toolbar's, and ⌘R) reads: `.all`, and what `.all` leaves out that
    /// the section showing reads (the Phone's Twilio and tunnel reads, Plugins' ssh hosts).
    public func refreshShown() async {
        await refresh(Part.everyday + Part.shown(with: selection))
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

    /// How many times each part has been asked for, so two overlapping reads of the same
    /// part can be told apart: bumped when one starts, and the result is kept only when it
    /// is still the latest one asked for — an older read that finishes last is dropped
    /// rather than overwriting what a newer one already stored.
    @ObservationIgnored private var sequences: [AnyKeyPath: Int] = [:]

    private func load<Value: Sendable>(
        _ path: ReferenceWritableKeyPath<AppModel, Loadable<Value>>, _ current: Int,
        _ fetch: @Sendable () async throws -> Value
    ) async {
        self[keyPath: path].begin()
        let sequence = (sequences[path] ?? 0) + 1
        sequences[path] = sequence
        do {
            let value = try await fetch()
            guard current == generation, sequences[path] == sequence else { return }
            self[keyPath: path].succeed(value)
        } catch {
            guard current == generation, sequences[path] == sequence else { return }
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
        case .setupPlan: setupPlan = .idle
        case .pin: pin = .idle
        case .service: service = .idle
        case .phoneNumbers: phoneNumbers = .idle
        case .phoneWebhooks: phoneWebhooks = .idle
        case .phoneTunnel: phoneTunnel = .idle
        case .integrations: integrations = .idle
        case .plugins: plugins = .idle
        case .tools: tools = .idle
        case .sshHosts: sshHosts = .idle
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

    /// Removes several settings from the store in one command (a card's, to disconnect it).
    public func unset(keys: [String]) async -> KeryxError? {
        guard !keys.isEmpty else { return nil }
        return await writeSetting { try await $0.unset(keys: keys) }
    }

    /// Turns the approval calls on or off (the kill switch; no restart needed).
    public func setEscalation(enabled: Bool) async -> KeryxError? {
        if let error = await perform({ try await $0.setEscalation(enabled: enabled) }, writing: .setting) {
            return error
        }
        await refresh([.approvals, .status])
        return nil
    }

    /// Restarts the service, then reads its status again a few seconds later. A refusal
    /// because calls are in progress comes back as `.liveCalls`.
    public func restart(force: Bool) async -> KeryxError? {
        if let error = await perform({ try await $0.restart(force: force) }, writing: .restart) { return error }
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
        }, writing: .model) { return error }
        if restartNeeded { configNeedsRestart = true }
        invalidateOptions()
        await refresh([.models, .config])
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
        if let error = await perform({ restartNeeded = try await $0.useVoiceOpenAI().restartNeeded },
                                     writing: .voice) {
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
        }, writing: .voice) { return .failure(error) }
        guard let answer else { return .failure(.launch("Keryx said nothing.")) }
        if answer.saved != nil {
            if answer.restartNeeded { configNeedsRestart = true }
            invalidateOptions()
            await refresh([.voice, .config])
        }
        return .success(answer)
    }

    /// Sets up (or restarts) the voice server on this Mac, one setup at a time, and reads
    /// the voice again once it has worked. `voice`: a Kokoro voice to switch to. Not public:
    /// the way in is `setUpThisMac`, which starts the model server first when it must.
    func setUpVoiceHere(voice: String? = nil) {
        guard let client else { return }
        // The setup fetches the speech models too: never beside a download of them. Reached
        // from a model server that has just come up, so said rather than silently dropped.
        guard !voiceDownloadRun.isBusy else {
            voiceSetup.refuse(StreamFailure(Self.speechModelsDownloading))
            return
        }
        voiceSetup.start(voice: voice, client: client) { [weak self] in
            self?.configNeedsRestart = true
            self?.invalidateOptions()
            Task { await self?.refresh([.voice, .config, .models]) }
        }
    }

    // MARK: - Setup

    /// Shows `request`'s section and holds the request for the screen there to answer; a
    /// later request replaces one not yet answered.
    public func request(_ request: AppRequest) {
        selection = request.destination
        requested = request
    }

    /// The screen has answered `request` (or will not): it is no longer pending. A different
    /// request asked for since is left alone.
    public func didHandle(_ request: AppRequest) {
        if requested == request { requested = nil }
    }

    /// The setup wizard is opening in `mode` (its window is opened by the caller).
    public func openSetupWizard(_ mode: SetupWizardMode) {
        wizardOpening = WizardOpening(mode: mode)
    }

    /// Records a setup step as walked (an optional one skipped, or the PIN left to the first
    /// call), then reads the plan again.
    public func markSetupStep(_ step: String) async -> KeryxError? {
        if let error = await perform({ _ = try await $0.markSetupStep(step) }, writing: .setupStep) { return error }
        await refresh(.setupPlan)
        return nil
    }

    /// Sets the phone PIN (or replaces it), the digits on the CLI's stdin. The caller has
    /// had them typed twice and compared them; a running Keryx keeps its old PIN until it
    /// restarts, which `restartNeeded` says.
    public func setPin(_ pin: String, replace: Bool) async -> Result<PinSetResult, KeryxError> {
        let answer = await answer(writing: .pin) { try await $0.setPin(pin, replace: replace) }
        if case let .success(result) = answer {
            if result.restartNeeded { configNeedsRestart = true }
            await refresh(.pin, .setupPlan)
        }
        return answer
    }

    /// Whether `value` works for `key`, before it is saved; it reaches the CLI on stdin.
    public func verify(key: String, value: String) async -> Result<VerifyResult, KeryxError> {
        await answer { try await $0.verify(key: key, value: value) }
    }

    /// Whether a Twilio account SID and auth token work together, both unsaved.
    public func verifyTwilio(accountSID: String, authToken: String) async -> Result<VerifyResult, KeryxError> {
        await answer { try await $0.verifyTwilio(accountSID: accountSID, authToken: authToken) }
    }

    // MARK: - Setup sections

    /// "About you": its questions, and the memory they would write.
    public func profileQuestions() async -> Result<ProfileQuestions, KeryxError> {
        await answer { try await $0.profileQuestions() }
    }

    /// What the answers would come to, writing nothing.
    public func previewProfile(_ answers: ProfileAnswers) async -> Result<ProfileResult, KeryxError> {
        await answer { try await $0.previewProfile(answers) }
    }

    /// Writes the first memory from the answers, then reads the memory and the plan again. A
    /// refusal is a result with its problems.
    public func applyProfile(_ answers: ProfileAnswers) async -> Result<ProfileResult, KeryxError> {
        let answer = await answer(writing: .memory) { try await $0.applyProfile(answers) }
        if case .success = answer { await refresh(.memory, .setupPlan) }
        return answer
    }

    /// What moving Jarvis's files would do, touching nothing.
    public func migrationPlan() async -> Result<MigrationPlan, KeryxError> {
        await answer { try await $0.migrationPlan() }
    }

    /// Moves Jarvis's files in `migrationRun`, one run at a time, and reads everything
    /// again once they have moved. False when one is already going.
    @discardableResult
    public func migrate() -> Bool {
        guard let client else { return false }
        return migrationRun.start(reason: "Moving Keryx's files", { client.migrate() }) { [weak self] _ in
            Task { await self?.refresh(.all) }
        }
    }

    /// The migration may still be stopped: it runs and no step has come. From its first step
    /// the CLI finishes the move whatever it is told, and a kill would leave it half done.
    public var canCancelMigration: Bool {
        migrationRun.isRunning && migrationRun.lastStep.isEmpty
    }

    /// Stops the migration, only while `canCancelMigration`.
    public func cancelMigration() {
        guard canCancelMigration else { return }
        migrationRun.cancel()
    }

    /// Project context's question, and the briefs kept already.
    public func projectContextQuestion() async -> Result<ProjectContextQuestion, KeryxError> {
        await answer { try await $0.projectContextQuestion() }
    }

    /// Has the agent draft the projects' briefs in `projectDraftRun`, one at a time; nothing
    /// is kept until `applyProjectContext`. False when one is already going.
    @discardableResult
    public func draftProjectContext(_ scope: ProjectScope) -> Bool {
        guard let client else { return false }
        return projectDraftRun.start(reason: "Reading your projects", { client.draftProjectContext(scope) })
    }

    /// Keeps what the owner kept of the draft, then reads the settings, the memory and the
    /// plan again. A problem is a result.
    public func applyProjectContext(_ keep: ProjectContextKeep) async -> Result<ProjectContextResult, KeryxError> {
        let answer = await answer(writing: .projects) { try await $0.applyProjectContext(keep) }
        if case .success = answer { await refresh(.config, .memory, .setupPlan) }
        return answer
    }

    /// SEAM: runs `agent`'s test task, once, in `smokeRun`: false — starting nothing — when
    /// the client cannot (no `auth smoke` yet), or one has run or is running.
    @discardableResult
    public func smokeTest(_ agent: String) -> Bool {
        guard smokeRun.phase == nil, let client = client as? AgentSmokeTesting else { return false }
        return smokeRun.start(reason: "Running the agent's test task", { client.smokeTest(agent) })
    }

    // MARK: - Phone

    /// Homebrew's `brew`, when this Mac has one: Cloudflare Tunnel can be installed from the app.
    public var homebrew: String? { client?.homebrew() }

    /// Installs Cloudflare Tunnel with Homebrew, then asks Keryx whether it finds it. Only
    /// ever after the owner's click.
    public func installTunnel() {
        guard let client, let brew = client.homebrew() else { return }
        tunnelInstall.start(client.installCloudflared(brew: brew)) { [weak self] in
            guard let self else { return false }
            await refresh(.phoneTunnel, .setupPlan, .doctor)
            return phoneTunnel.value?.kind != nil || client.cloudflaredInstalled()
        }
    }

    /// Cloudflare Tunnel is installed where Homebrew puts it, but Keryx's own PATH does not
    /// reach it: the tunnel reads as missing to Keryx though the install worked.
    public var tunnelOutsideKeryxPath: Bool {
        guard phoneTunnel.value?.kind == nil, let client else { return false }
        return client.cloudflaredInstalled()
    }

    // MARK: - Phone

    /// Reads the three phone documents (numbers, webhooks, tunnel) side by side.
    public func refreshPhone() async {
        await refresh(.phoneNumbers, .phoneWebhooks, .phoneTunnel)
    }

    /// Asks Twilio where `TWILIO_NUMBER`'s webhooks point, for the question before changing
    /// them: this answer itself, so the question shows only what was read for it — never a
    /// report from before, nor one a slower read lands later. The Phone section's copy is
    /// brought up to date with it.
    public func freshPhoneWebhooks() async -> Result<PhoneWebhooks, KeryxError> {
        let current = generation
        let answer = await answer { try await $0.phoneWebhooks() }
        if case let .success(report) = answer, current == generation { phoneWebhooks.succeed(report) }
        return answer
    }

    /// Points `TWILIO_NUMBER` here — only after the owner has seen both addresses and said
    /// yes — then reads the phone documents and the plan again.
    public func applyPhoneWebhooks() async -> Result<WebhooksApplied, KeryxError> {
        let answer = await answer(writing: .webhooks) { try await $0.applyPhoneWebhooks() }
        if case .success = answer { await refresh(.phoneWebhooks, .phoneNumbers, .setupPlan) }
        return answer
    }

    // MARK: - Service

    /// Installs Keryx and its tunnel as services, then reads the units and the status again.
    /// Refused while an install or uninstall is already under way, from whichever window.
    public func installService() async -> Result<ServiceResult, KeryxError> {
        await changeService(.installing, reread: [.service, .status, .setupPlan]) {
            try await $0.installService()
        }
    }

    /// Removes Keryx and its tunnel; refused as `installService` is.
    public func uninstallService() async -> Result<ServiceResult, KeryxError> {
        await changeService(.uninstalling, reread: [.service, .status, .models, .voice, .setupPlan]) {
            try await $0.uninstallService()
        }
    }

    // MARK: - Answering calls

    /// What keeping Keryx answering calls asks for now (`answeringStep(plan:status:)`).
    public var answeringStep: AnsweringStep {
        Self.answeringStep(plan: setupPlan.value, status: status.value)
    }

    /// Whether Keryx should be started, from the plan and the status: `.start` only once
    /// setup has reached the service step (not before a migration, nor while a step it waits
    /// for — the phone, the voice, the agents — is left), only with a service manager to
    /// start it under, and never beside a Keryx started by hand, which already answers.
    nonisolated public static func answeringStep(plan: SetupPlan?, status: StatusReport?) -> AnsweringStep {
        guard let status, status.storage.ok, !status.storage.migrationRequired, status.service.ok,
              status.service.manager != nil else { return .unknown }
        if status.service.installed && status.service.active { return .running }
        // Answering without the unit: `keryx serve` started by hand. One Keryx at a time.
        if status.health.reachable { return .running }
        guard let plan else { return .unknown }
        if let migrate = plan.step("migrate"), migrate.kind != .done { return .waitingForSetup }
        guard let step = plan.step("service") else { return .unknown }
        switch step.kind {
        case .needed?, .done?: return .start
        // Installed but stopped: the CLI fails the step on its "service running" check, and
        // the fix is the start. Failed for anything else (a security check), it waits.
        case .failed?: return step.fix == Self.startFix ? .start : .waitingForSetup
        default: return .waitingForSetup
        }
    }

    /// The fix id (`keryx/fixes.py`) that is Keryx's unit started.
    nonisolated static let startFix = "service:install"

    /// Starts Keryx when setup allows and it isn't running, once a launch; does nothing
    /// unless `keepsAnswering`. Reads the plan and the status first, so it decides on what is
    /// true now. A check already under way is waited for rather than run twice.
    public func ensureAnswering() async {
        if ensuring == nil { checkAnswering() }
        await ensuring?.value
    }

    private func checkAnswering() {
        guard ensuring == nil else { return }
        ensuring = Task { [weak self] in
            await self?.ensureOnce()
            self?.ensuring = nil
        }
    }

    private func ensureOnce() async {
        guard keepsAnswering, !quitting, client != nil, !startTried, serviceChange == nil else { return }
        let current = generation
        await refresh([.setupPlan, .status])
        guard current == generation, keepsAnswering, !quitting, !startTried, serviceChange == nil,
              answeringStep == .start else { return }
        _ = await startAnswering()
    }

    /// Starts Keryx answering calls: its unit installed (or, installed, started again) with
    /// `keryx service install`, then the status read once more after a moment, as after a
    /// restart. A failure is kept in `startFailure` for the Overview to say.
    public func startAnswering() async -> Result<ServiceResult, KeryxError> {
        if quitting { return .failure(.refused(Self.quittingRefusal)) }
        if client != nil, serviceChange == nil { startTried = true }
        let answer = await installService()
        switch answer {
        case .success:
            startFailure = nil
            try? await Task.sleep(for: timing.afterRestart)
            await refresh(.status)
        case .failure(.refused(Self.serviceBusy)):
            break
        case let .failure(error):
            startFailure = error
        }
        return answer
    }

    /// Whether `keepsAnswering` should be on for an app started with `environment`: not for
    /// a sandbox run (`KERYX_APP_ENVIRONMENT=inherit`), whose `keryx` reads the sandbox's
    /// settings but would install, or remove, the real launch agents in `~/Library`.
    nonisolated public static func keepsAnswering(environment: [String: String]) -> Bool {
        environment[KeryxLocator.environmentOverride] != "inherit"
    }

    /// The app is quitting: the status poll stops, and nothing starts Keryx again — not the
    /// automatic start, not Start — before `stopAnswering` stops it.
    public func prepareToQuit() {
        quitting = true
        stop()
    }

    static let quittingRefusal = "Keryx is quitting."

    /// Whether quitting now stops Keryx answering calls: kept answering, and its unit is
    /// installed (or being installed).
    public var quitStopsAnswering: Bool {
        guard keepsAnswering, client != nil else { return false }
        return serviceChange == .installing || service.value?.units.agent.installed == true
            || status.value?.service.installed == true
    }

    /// Stops Keryx answering calls, as the app quits: its unit removed with `keryx service
    /// uninstall`, which is the CLI's only way to stop it (the next launch installs it
    /// again). An install still finishing is waited for first, briefly. Nothing unless
    /// `keepsAnswering`, and nothing when no unit is installed — a Keryx started by hand is
    /// not the app's to stop. Reads nothing after: the app is going.
    @discardableResult
    public func stopAnswering() async -> KeryxError? {
        guard keepsAnswering, client != nil else { return nil }
        let deadline = ContinuousClock.now + .seconds(5)
        while serviceChange != nil, ContinuousClock.now < deadline {
            try? await Task.sleep(for: .milliseconds(50))
        }
        await refresh(.service)
        let installed = service.value?.units.agent.installed ?? status.value?.service.installed ?? false
        guard installed else { return nil }
        if case let .failure(error) = await changeService(.uninstalling, reread: [], Self.stopUnits) {
            return error
        }
        return nil
    }

    /// How Quit stops Keryx's units — the one place to change when the CLI has a stop that
    /// keeps them installed (`keryx service stop --all`, asked for): today it is `keryx
    /// service uninstall`, its only stop, and launch installs them again.
    private static let stopUnits: @MainActor (KeryxClient) async throws -> ServiceResult = {
        try await $0.uninstallService()
    }

    /// One service change at a time, run in `serviceTask` so quitting can stop it.
    private func changeService(_ change: ServiceChange, reread: [Part],
                               _ action: @escaping @MainActor (KeryxClient) async throws -> ServiceResult) async
        -> Result<ServiceResult, KeryxError> {
        guard serviceChange == nil else { return .failure(.refused(Self.serviceBusy)) }
        serviceChange = change
        let task = Task { await self.answer(action) }
        serviceTask = task
        let answer = await task.value
        serviceTask = nil
        serviceChange = nil
        if case .success = answer { await refresh(reread) }
        return answer
    }

    /// The model server may be started now: no start is under way, no stop, and no voice
    /// setup that runs on it.
    public var canStartModelServer: Bool {
        client != nil && !modelServerRun.isBusy && !voiceSetup.isBusy && !stoppingModelServer
    }

    /// Starts the model server here (`keryx models start`), one run at a time, and reads the
    /// models, the units and the plan again once it serves the chosen model. False, starting
    /// nothing, unless `canStartModelServer`.
    @discardableResult
    public func runModelServer(runtime: String? = nil) -> Bool {
        guard canStartModelServer else { return false }
        // Started on its own, it is no part of a This Mac setup.
        guard beginModelServer(runtime: runtime, then: nil) else { return false }
        thisMacSetup = nil
        return true
    }

    /// `runModelServer`, with `then` run once it serves (This Mac's Set Up… goes on to the
    /// voice) — unless it was stopped meanwhile; false when a run was already going.
    private func beginModelServer(runtime: String?, then: (@MainActor () -> Void)?) -> Bool {
        guard let client else { return false }
        let token = then == nil ? nil : UUID()
        let started = modelServerRun.start(reason: "Starting the model server",
                                           { client.startModelServer(runtime: runtime) }) { [weak self] _ in
            guard let self else { return }
            invalidateOptions()
            // Read again whatever comes next: the server is up.
            Task { await self.refresh(.models, .service, .voice, .config, .setupPlan) }
            // A stop asked for, even one the `done` beat, ends the chain here.
            if let token, chain == token {
                chain = nil
                then?()
            }
        }
        if started { chain = token }
        return started
    }

    /// Set Up… (or Restart Voice Server) on This Mac: the model server first when its words
    /// would come from one that isn't answering — `startServer` says so outright, else the
    /// models as last read do — then the voice. `runtime`: the model server's, `ollama` for
    /// Ollama's copy of the model. Nothing starts while either is busy, nor while the
    /// speech models download (the setup would fetch them too).
    public func setUpThisMac(voice: String? = nil, startServer: Bool? = nil, runtime: String? = nil) {
        guard canStartModelServer, !voiceDownloadRun.isBusy, !voiceLLMBusy else { return }
        let first = startServer ?? ((models.value?.modelServerDown ?? false) && voiceWordsAreTheAgents)
        thisMacSetup = ThisMacSetup(startsServer: first, runtime: first ? runtime : nil)
        voiceSetup.dismiss()
        if first {
            _ = beginModelServer(runtime: runtime) { [weak self] in self?.setUpVoiceHere(voice: voice) }
        } else {
            setUpVoiceHere(voice: voice)
        }
    }

    /// The voice on this Mac takes its words from the local agent's model (`VOICE_LLM`
    /// shared, or a CLI too old to say): only then does its setup wait on the agent's model
    /// server.
    public var voiceWordsAreTheAgents: Bool {
        (voice.value?.llmSource ?? .shared) == .shared
    }

    // MARK: - The voice LLM

    /// The voice LLM is being chosen, or its own server started or stopped.
    public var voiceLLMBusy: Bool {
        voiceLLMRun.isBusy || voiceLLMServerRun.isBusy || stoppingVoiceLLMServer
    }

    /// Where the voice's words come from may be changed now: nothing else is changing it,
    /// and no voice setup — which reads it — runs or waits for its model server.
    public var canChangeVoiceLLM: Bool {
        client != nil && !voiceLLMBusy && !voiceSetup.isBusy && chain == nil
    }

    /// Makes `choice` where a voice on this Mac takes its words from, in `voiceLLMRun`, one
    /// at a time (`keryx voice llm`), and reads the voice, the settings, the models and the
    /// units again once it is done. False, starting nothing, unless `canChangeVoiceLLM`.
    @discardableResult
    public func chooseVoiceLLM(_ choice: VoiceLLMChoice) -> Bool {
        guard canChangeVoiceLLM, let client else { return false }
        let started = voiceLLMRun.start(reason: "Choosing the voice LLM", { client.chooseVoiceLLM(choice) }) {
            [weak self] _ in
            self?.invalidateOptions()
            Task { await self?.refresh(.voice, .config, .models, .service, .setupPlan) }
        }
        if started {
            if case let .remote(url, name, .typed) = choice {
                voiceLLMChoice = .remote(url: url, model: name, key: .stored)
            } else {
                voiceLLMChoice = choice
            }
        }
        return started
    }

    /// Starts the voice LLM's own server on the model chosen for it (`keryx models start
    /// --voice-llm`), one run at a time, and reads the voice and the units again once it
    /// serves. False, starting nothing, unless `canChangeVoiceLLM`.
    @discardableResult
    public func runVoiceLLMServer() -> Bool {
        guard canChangeVoiceLLM, let client else { return false }
        return voiceLLMServerRun.start(reason: "Starting the voice LLM's server",
                                       { client.startVoiceLLMServer() }) { [weak self] _ in
            Task { await self?.refresh(.voice, .service, .setupPlan) }
        }
    }

    /// Stops the voice LLM's own server and removes its unit. Refused while the voice LLM
    /// is being chosen or its server started, and while the voice is being set up.
    public func stopVoiceLLMServer() async -> Result<ServiceResult, KeryxError> {
        if stoppingVoiceLLMServer { return .failure(.refused(Self.voiceLLMServerStopping)) }
        if voiceLLMRun.isBusy || voiceLLMServerRun.isBusy || voiceSetup.isBusy {
            return .failure(.refused(Self.voiceLLMServerInUse))
        }
        stoppingVoiceLLMServer = true
        defer { stoppingVoiceLLMServer = false }
        let answer = await answer(writing: .voiceLLMServerStop) { try await $0.stopVoiceLLMServer() }
        if case .success = answer { await refresh(.voice, .service, .setupPlan) }
        return answer
    }

    /// Cancel on This Mac's setup: stops whichever of its runs is going, and the voice never
    /// follows the model server.
    public func cancelThisMacSetup() {
        if modelServerRun.isRunning { cancelModelServer() } else { voiceSetup.cancel() }
    }

    /// Stops the model server's start — and, when This Mac's setup began with it, the voice
    /// it would have gone on to.
    public func cancelModelServer() {
        chain = nil
        modelServerRun.cancel()
    }

    /// Forgets a This Mac setup that failed or finished: both of its runs.
    public func dismissThisMacSetup() {
        if thisMacSetup?.startsServer == true { modelServerRun.dismiss() }
        voiceSetup.dismiss()
    }

    /// Stops the model server and removes its unit. Refused while it is starting or
    /// already stopping, and while the voice is being set up on it.
    public func stopModelServer() async -> Result<ServiceResult, KeryxError> {
        if stoppingModelServer { return .failure(.refused(Self.modelServerStopping)) }
        if modelServerRun.isBusy || voiceSetup.isBusy { return .failure(.refused(Self.modelServerInUse)) }
        stoppingModelServer = true
        defer { stoppingModelServer = false }
        let answer = await answer(writing: .modelServerStop) { try await $0.stopModelServer() }
        if case .success = answer { await refresh(.models, .service, .voice) }
        return answer
    }

    /// The speech models may be downloaded now: no download is going, and no voice setup —
    /// which fetches them too — is running or waiting for its model server.
    public var canDownloadVoiceModels: Bool {
        client != nil && !voiceDownloadRun.isBusy && !voiceSetup.isBusy && chain == nil
    }

    /// Downloads the voice's speech models ahead of its first start, one run at a time,
    /// and reads the voice again once they are in. False, starting nothing, unless
    /// `canDownloadVoiceModels`.
    @discardableResult
    public func downloadVoiceModels() -> Bool {
        guard canDownloadVoiceModels, let client else { return false }
        return voiceDownloadRun.start(reason: "Downloading the voice's speech models",
                                      { client.downloadVoiceModels() }) { [weak self] _ in
            Task { await self?.refresh(.voice) }
        }
    }

    // MARK: - Accounts

    /// Signs in to `provider` in `signInRun`, one sign-in at a time: false when one is
    /// already going. Its `open` is the page to open (through `TrustedLink`), its `send`
    /// takes the one line Claude's and Gmail's ask for; the others' stdin is closed at once.
    /// Once it has worked the accounts are read again; a token stored as a setting
    /// (Claude's) is loaded by the next restart.
    @discardableResult
    public func signIn(_ provider: SignInProvider, clientFile: String? = nil) -> Bool {
        guard let client else { return false }
        let takes: StreamRun<SignedIn>.Input = provider.readsALine ? .oneLine : .none
        let started = signInRun.start(reason: "Signing in", takes: takes,
                                      { client.signIn(provider, clientFile: clientFile) }) {
            [weak self] done in
            if done.stored != nil { self?.configNeedsRestart = true }
            self?.invalidateOptions()
            Task { await self?.refresh(.integrations, .plugins, .tools, .config, .doctor, .setupPlan) }
        }
        if started { lastSignIn = provider }
        return started
    }

    /// Signs out of Gmail or Google for agents, then reads the accounts again.
    public func signOut(_ provider: SignInProvider) async -> Result<SignOutResult, KeryxError> {
        let answer = await answer(writing: .signOut) { try await $0.signOut(provider) }
        if case .success = answer { await refresh(.integrations, .plugins, .tools, .setupPlan) }
        return answer
    }

    // MARK: - Plugins

    /// Turns a plugin on with `values` written first; from the next call, no restart.
    public func installPlugin(_ name: String, values: PluginValues = PluginValues()) async
        -> Result<PluginResult, KeryxError> {
        await pluginChange { try await $0.installPlugin(name, values: values) }
    }

    /// Turns a plugin off; its settings are kept.
    public func removePlugin(_ name: String) async -> Result<PluginResult, KeryxError> {
        await pluginChange { try await $0.removePlugin(name) }
    }

    /// Changes an installed plugin's settings.
    public func setPlugin(_ name: String, values: PluginValues) async -> Result<PluginResult, KeryxError> {
        await pluginChange { try await $0.setPlugin(name, values: values) }
    }

    private func pluginChange(_ change: (KeryxClient) async throws -> PluginResult) async
        -> Result<PluginResult, KeryxError> {
        let answer = await answer(writing: .plugin, change)
        if case .success = answer { await refresh(.plugins, .tools, .setupPlan) }
        return answer
    }

    // MARK: - Work under way

    /// Something long-running is going or still stopping: a download, a voice setup, a
    /// model server start or stop, a speech model download, a sign-in, Keryx's service being
    /// installed or uninstalled.
    public var isBusy: Bool {
        downloads.isBusy || voiceSetup.isBusy || modelServerRun.isBusy || voiceDownloadRun.isBusy || voiceLLMBusy
            || signInRun.isBusy || serviceChange != nil || stoppingModelServer
            || migrationRun.isBusy || projectDraftRun.isBusy || smokeRun.isBusy || tunnelInstall.isBusy
    }

    /// Asks what this model runs beside the downloads and the voice setup to stop (the model
    /// server start, the speech model download, a sign-in, a service install or uninstall).
    public func cancelRuns() {
        // Nothing a stopped run would have gone on to starts.
        chain = nil
        modelServerRun.cancel()
        voiceLLMRun.cancel()
        voiceLLMServerRun.cancel()
        voiceDownloadRun.cancel()
        signInRun.cancel()
        projectDraftRun.cancel()
        smokeRun.cancel()
        tunnelInstall.cancel()
        // Not the migration once a step has come: the CLI finishes the move first.
        cancelMigration()
        serviceTask?.cancel()
    }

    static let serviceBusy = "Keryx is already starting or stopping. Wait for that to finish."
    static let modelServerStopping = "The model server is already stopping."
    static let modelServerInUse =
        "The model server can't be stopped while it is starting or while the voice is being set up on it."
    static let voiceLLMServerStopping = "The voice LLM's server is already stopping."
    static let voiceLLMServerInUse =
        "The voice LLM's server can't be stopped while it is starting, while the voice LLM is being chosen, "
        + "or while the voice is being set up."
    static let speechModelsDownloading =
        "The speech models are still downloading. Set it up again once they're in."

    private func writeSetting(_ write: (KeryxClient) async throws -> Void) async -> KeryxError? {
        if let error = await perform(write, writing: .setting) { return error }
        configNeedsRestart = true
        await refresh(.config)
        return nil
    }

    /// `action`'s answer, or its error as a `KeryxError`. `writing`: what it changes, listed
    /// in `writes` while it runs.
    private func answer<Value>(writing: Write? = nil, _ action: (KeryxClient) async throws -> Value) async
        -> Result<Value, KeryxError> {
        var value: Value?
        if let error = await perform({ value = try await action($0) }, writing: writing) { return .failure(error) }
        guard let value else { return .failure(.launch("Keryx said nothing.")) }
        return .success(value)
    }

    private func perform(_ action: (KeryxClient) async throws -> Void, writing: Write? = nil) async -> KeryxError? {
        guard let client else {
            return location.error ?? .notFound(LocateFailure(lookedAt: []))
        }
        let id = UUID()
        if let writing { writes[id] = writing }
        defer { writes[id] = nil }
        do {
            try await action(client)
            return nil
        } catch {
            return Self.keryxError(error)
        }
    }
}

/// What keeping Keryx answering calls asks for (`AppModel.answeringStep`).
public enum AnsweringStep: Sendable, Equatable {
    /// Not known yet: no status or plan read, or nothing here to start it under.
    case unknown
    /// Setup has not reached the step that starts it: a migration, or a step it waits for.
    case waitingForSetup
    /// It should be started.
    case start
    /// It answers already: its unit running, or a Keryx started by hand.
    case running
}
