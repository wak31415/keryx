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
        var voice: Result<VoiceStatus, KeryxError> = .success(PreviewData.voice)
        var options: @Sendable (String) -> Result<ConfigOptions, KeryxError> = { key in
            .success(PreviewData.options[key] ?? PreviewData.builtinOptions(key))
        }
        var voiceOpenAI: Result<VoiceUseResult, KeryxError> = .success(VoiceUseResult(saved: [:], restartNeeded: true))
        var voiceServer: Result<VoiceServerResult, KeryxError> = .success(PreviewClient.serverSaved)
        /// Awaited before each `config options` answer: a test's way to hold them.
        var optionsWait: @Sendable () async -> Void = {}
        /// Awaited before `service install|uninstall` answers, to hold one mid-way.
        var serviceWait: @Sendable () async -> Void = {}
        /// Awaited before `models stop` answers, to hold one mid-way.
        var stopWait: @Sendable () async -> Void = {}
        var voiceHere: @Sendable (String?) -> VoiceSetupStream = { _ in
            VoiceSetupStream(AsyncThrowingStream { $0.finish() })
        }
        var setupPlan: Result<SetupPlan, KeryxError> = .success(PreviewData.setupPlanPartway)
        var pin: Result<PinStatus, KeryxError> = .success(PreviewData.pinFile)
        var pinSet: Result<PinSetResult, KeryxError> = .success(PinSetResult(set: true, replaced: false,
                                                                             restartNeeded: true))
        var verify: Result<VerifyResult, KeryxError> = .success(PreviewClient.verified)
        var service: Result<ServiceReport, KeryxError> = .success(PreviewData.service)
        var serviceResult: Result<ServiceResult, KeryxError> = .success(ServiceResult(ok: true, message: "done"))
        var phoneNumbers: Result<PhoneNumbers, KeryxError> = .success(PreviewData.phoneNumbers)
        var phoneWebhooks: Result<PhoneWebhooks, KeryxError> = .success(PreviewData.phoneWebhooks)
        var applied: Result<WebhooksApplied, KeryxError> = .success(WebhooksApplied(applied: true,
                                                                                     number: "+15550001111"))
        var phoneTunnel: Result<PhoneTunnel, KeryxError> = .success(PreviewData.phoneTunnel)
        var integrations: Result<IntegrationsReport, KeryxError> = .success(PreviewData.integrations)
        var signOut: Result<SignOutResult, KeryxError> = .success(SignOutResult(name: "gmail", signedIn: false,
                                                                                message: "Signed out."))
        var plugins: Result<PluginsReport, KeryxError> = .success(PreviewData.plugins)
        var plugin: Result<PluginResult, KeryxError> = .success(PluginResult(name: "check_billing", on: true,
                                                                             message: "Billing is on."))
        var tools: Result<ToolsReport, KeryxError> = .success(PreviewData.tools)
        var sshHosts: Result<SshHostsReport, KeryxError> = .success(PreviewData.sshHosts)
        var modelServer: @Sendable (String?) -> ModelServerStream = { _ in
            ModelServerStream(AsyncThrowingStream { $0.finish() })
        }
        var voiceDownload: @Sendable () -> VoiceDownloadStream = {
            VoiceDownloadStream(AsyncThrowingStream { $0.finish() })
        }
        var signIn: @Sendable (SignInProvider, String?) -> SignInStream = { _, _ in
            SignInStream(AsyncThrowingStream { $0.finish() })
        }
        var profile: Result<ProfileQuestions, KeryxError> = .success(PreviewData.profileQuestions)
        var profileResult: Result<ProfileResult, KeryxError> = .success(PreviewClient.profileWritten)
        var migrationPlan: Result<MigrationPlan, KeryxError> = .success(PreviewData.migrationPlan)
        var migrate: @Sendable () -> MigrationStream = { MigrationStream(AsyncThrowingStream { $0.finish() }) }
        var projectQuestion: Result<ProjectContextQuestion, KeryxError> = .success(PreviewData.projectContextQuestion)
        var projectDraft: @Sendable (ProjectScope) -> ProjectDraftStream = { _ in
            ProjectDraftStream(AsyncThrowingStream { $0.finish() })
        }
        var projectResult: Result<ProjectContextResult, KeryxError> = .success(PreviewClient.projectsKept)
        var homebrew: String?
        var brewInstall: @Sendable () -> StoppableStream<String> = {
            StoppableStream(AsyncThrowingStream { $0.finish() })
        }
        /// The answers each `setup profile --preview|--apply` and project-context `--apply`
        /// was handed on stdin, in order.
        var piped: [Data] = []
    }

    /// What was piped to the setup sections' commands, in order.
    var piped: [Data] { lock.withLock { replies.piped } }

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
    func config() async throws -> [ConfigRow] { try answer("config list") { $0.config } }
    func set(key: String, value: String) async throws { try write("config set \(key) \(value)") }
    func setSecret(key: String, value: String) async throws { try write("config set \(key) --stdin") }
    func unset(key: String) async throws { try write("config unset \(key)") }
    func unset(keys: [String]) async throws { try write("config unset \(keys.joined(separator: " "))") }
    func approvals(limit: Int) async throws -> ApprovalsReport { try answer("approvals") { $0.approvals } }
    func setEscalation(enabled: Bool) async throws {
        try write("approvals \(enabled ? "--enable" : "--disable")")
    }
    func memory() async throws -> MemoryDocument { try answer("memory") { $0.memory } }
    func restart(force: Bool) async throws { try write("restart\(force ? " --force" : "")") }
    func options(key: String) async throws -> ConfigOptions {
        let wait = lock.withLock { replies.optionsWait }
        await wait()
        return try answer("config options \(key)") { $0.options(key) }
    }
    func voice() async throws -> VoiceStatus { try answer("voice") { $0.voice } }
    func useVoiceOpenAI() async throws -> VoiceUseResult { try answer("voice use openai") { $0.voiceOpenAI } }
    func useVoiceServer(url: String, key: ServerKey, keepAnyway: Bool) async throws -> VoiceServerResult {
        let flags = (key.toSend == nil ? "" : " --key-stdin") + (key == .clear ? " --clear-key" : "")
            + (keepAnyway ? " --keep-anyway" : "")
        return try answer("voice use server \(url)\(flags)") { $0.voiceServer }
    }
    func voiceHere(voice: String?) -> VoiceSetupStream {
        let make = lock.withLock {
            recorded.append("voice use here" + (voice.map { " --voice \($0)" } ?? ""))
            return replies.voiceHere
        }
        return make(voice)
    }

    // Setup, phone, service, accounts, plugins: recorded as the CLI words them, never a
    // secret (a PIN or a key is said as `--stdin`).
    func setupPlan() async throws -> SetupPlan { try answer("setup plan") { $0.setupPlan } }
    func markSetupStep(_ step: String) async throws -> SetupMarkResult {
        try write("setup mark \(step)")
        return SetupMarkResult(id: step, walked: true)
    }
    func pin() async throws -> PinStatus { try answer("pin") { $0.pin } }
    func setPin(_ pin: String, replace: Bool) async throws -> PinSetResult {
        try answer("pin set --stdin\(replace ? " --replace" : "")") { $0.pinSet }
    }
    func verify(key: String, value: String) async throws -> VerifyResult {
        try answer("config verify \(key) --stdin") { $0.verify }
    }
    func verifyTwilio(accountSID: String, authToken: String) async throws -> VerifyResult {
        try answer("config verify TWILIO_AUTH_TOKEN --stdin --pair") { $0.verify }
    }
    func phoneNumbers() async throws -> PhoneNumbers { try answer("phone numbers") { $0.phoneNumbers } }
    func phoneWebhooks() async throws -> PhoneWebhooks { try answer("phone webhooks") { $0.phoneWebhooks } }
    func applyPhoneWebhooks() async throws -> WebhooksApplied { try answer("phone webhooks --apply") { $0.applied } }
    func phoneTunnel() async throws -> PhoneTunnel { try answer("phone tunnel") { $0.phoneTunnel } }
    func service() async throws -> ServiceReport { try answer("service") { $0.service } }
    func installService() async throws -> ServiceResult {
        let wait = lock.withLock { replies.serviceWait }
        await wait()
        // A cancelled run throws, as the runner's does once it has stopped the group.
        try Task.checkCancellation()
        return try answer("service install") { $0.serviceResult }
    }
    func uninstallService() async throws -> ServiceResult {
        let wait = lock.withLock { replies.serviceWait }
        await wait()
        try Task.checkCancellation()
        return try answer("service uninstall") { $0.serviceResult }
    }
    func startModelServer(runtime: String?) -> ModelServerStream {
        let make = lock.withLock {
            recorded.append("models start" + (runtime.map { " --runtime \($0)" } ?? ""))
            return replies.modelServer
        }
        return make(runtime)
    }
    func stopModelServer() async throws -> ServiceResult {
        let wait = lock.withLock { replies.stopWait }
        await wait()
        return try answer("models stop") { $0.serviceResult }
    }
    func downloadVoiceModels() -> VoiceDownloadStream {
        let make = lock.withLock {
            recorded.append("voice download")
            return replies.voiceDownload
        }
        return make()
    }
    func signIn(_ provider: SignInProvider, clientFile: String?) -> SignInStream {
        let make = lock.withLock {
            recorded.append("auth login \(provider.rawValue)" + (clientFile == nil ? "" : " --client-file"))
            return replies.signIn
        }
        return make(provider, clientFile)
    }
    func signOut(_ provider: SignInProvider) async throws -> SignOutResult {
        try answer("auth logout \(provider.rawValue)") { $0.signOut }
    }
    func integrations() async throws -> IntegrationsReport { try answer("integrations") { $0.integrations } }
    func plugins() async throws -> PluginsReport { try answer("plugins list") { $0.plugins } }
    func installPlugin(_ name: String, values: PluginValues) async throws -> PluginResult {
        try answer("plugins install \(name)") { $0.plugin }
    }
    func removePlugin(_ name: String) async throws -> PluginResult { try answer("plugins remove \(name)") { $0.plugin } }
    func setPlugin(_ name: String, values: PluginValues) async throws -> PluginResult {
        try answer("plugins set \(name)") { $0.plugin }
    }
    func tools() async throws -> ToolsReport { try answer("tools") { $0.tools } }
    func homebrew() -> String? { lock.withLock { replies.homebrew } }
    func installCloudflared(brew: String) -> StoppableStream<String> {
        let make = lock.withLock {
            recorded.append("brew install cloudflared")
            return replies.brewInstall
        }
        return make()
    }
    func profileQuestions() async throws -> ProfileQuestions { try answer("setup profile") { $0.profile } }
    func previewProfile(_ answers: ProfileAnswers) async throws -> ProfileResult {
        try lock.withLock { replies.piped.append(try answers.json()) }
        return try answer("setup profile --preview") { $0.profileResult }
    }
    func applyProfile(_ answers: ProfileAnswers) async throws -> ProfileResult {
        try lock.withLock { replies.piped.append(try answers.json()) }
        return try answer("setup profile --apply") { $0.profileResult }
    }
    func migrationPlan() async throws -> MigrationPlan { try answer("migrate") { $0.migrationPlan } }
    func migrate() -> MigrationStream {
        let make = lock.withLock {
            recorded.append("migrate --yes")
            return replies.migrate
        }
        return make()
    }
    func projectContextQuestion() async throws -> ProjectContextQuestion {
        try answer("setup project-context --describe") { $0.projectQuestion }
    }
    func draftProjectContext(_ scope: ProjectScope) -> ProjectDraftStream {
        let make = lock.withLock {
            switch scope {
            case .explore: recorded.append("setup project-context --explore")
            case let .folders(paths): recorded.append("setup project-context --folder ×\(paths.count)")
            }
            return replies.projectDraft
        }
        return make(scope)
    }
    func applyProjectContext(_ keep: ProjectContextKeep) async throws -> ProjectContextResult {
        try lock.withLock { replies.piped.append(try keep.json()) }
        return try answer("setup project-context --apply") { $0.projectResult }
    }
    func sshHosts() async throws -> SshHostsReport { try answer("plugins hosts") { $0.sshHosts } }
}

/// A `StreamEvent` stream the test drives by hand, as `ManualSetup` drives a voice setup:
/// what it was sent, and how often it was asked to stop.
final class ManualRun<Done: StreamDone>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: AsyncThrowingStream<StreamEvent<Done>, Error>.Continuation?
    private var stops = 0
    private var lines: [String] = []
    private var takesInput = true

    var stopRequests: Int { lock.withLock { stops } }
    var sent: [String] { lock.withLock { lines } }
    var inputOpen: Bool { lock.withLock { takesInput } }

    /// Its input closed: `send` is refused from now on.
    func closeInput() { lock.withLock { takesInput = false } }

    func stream() -> StoppableStream<StreamEvent<Done>> {
        let events = AsyncThrowingStream<StreamEvent<Done>, Error> { continuation in
            lock.withLock { self.continuation = continuation }
        }
        let input = StreamInput(send: { [weak self] line in
            guard let self else { return false }
            return lock.withLock {
                guard takesInput else { return false }
                lines.append(line)
                return true
            }
        }, close: { [weak self] in self?.closeInput() })
        return StoppableStream(events, stop: { [weak self] in
            guard let self else { return }
            self.lock.withLock { self.stops += 1 }
        }, input: input)
    }

    func send(_ event: StreamEvent<Done>) { lock.withLock { continuation }?.yield(event) }
    func finish(throwing error: Error? = nil) { lock.withLock { continuation }?.finish(throwing: error) }
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

/// A voice setup stream the test drives by hand, as `ManualPull` drives a download.
final class ManualSetup: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: AsyncThrowingStream<VoiceSetupEvent, Error>.Continuation?
    private var stops = 0

    var stopRequests: Int { lock.withLock { stops } }

    func stream() -> VoiceSetupStream {
        let events = AsyncThrowingStream<VoiceSetupEvent, Error> { continuation in
            lock.withLock { self.continuation = continuation }
        }
        return VoiceSetupStream(events, stop: { [weak self] in
            guard let self else { return }
            self.lock.withLock { self.stops += 1 }
        })
    }

    func send(_ event: VoiceSetupEvent) { lock.withLock { continuation }?.yield(event) }
    func finish(throwing error: Error? = nil) { lock.withLock { continuation }?.finish(throwing: error) }
}

/// Holds `config options` answers until opened, counting how many wait at once. Opened, it
/// lets every held answer go and holds none after, so no test can leave one waiting.
final class OptionsGate: @unchecked Sendable {
    private let lock = NSLock()
    private var waiting: [CheckedContinuation<Void, Never>] = []
    private var isOpen = false
    private var most = 0

    var held: Int { lock.withLock { waiting.count } }
    var mostAtOnce: Int { lock.withLock { most } }

    func wait() async {
        await withCheckedContinuation { continuation in
            let pass = lock.withLock {
                if isOpen { return true }
                waiting.append(continuation)
                most = max(most, waiting.count)
                return false
            }
            if pass { continuation.resume() }
        }
    }

    func open() {
        let all = lock.withLock {
            isOpen = true
            defer { waiting = [] }
            return waiting
        }
        for continuation in all { continuation.resume() }
    }
}

/// Waits for something the model does in the background (the status poll, a download's
/// progress). It returns as soon as `condition` holds; the generous timeout is for a main
/// actor kept busy by the suites that render views, never a wait in the passing case.
@MainActor
func eventually(timeout: Duration = .seconds(180), _ condition: @MainActor () -> Bool) async -> Bool {
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
