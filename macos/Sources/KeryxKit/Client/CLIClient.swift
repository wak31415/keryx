import Foundation

/// Runs the `keryx` CLI where `location` says, as the owner at a terminal: argv is
/// `prefixArguments + subcommand`, the environment is sanitised (no actor variables), and
/// the working directory is the checkout, or `/` with none (`KeryxLocation.runDirectory`). A
/// secret only ever travels on stdin. Nothing here logs an argument, the environment, stdin
/// or output. A command whose caller was cancelled throws `CancellationError`, whatever the
/// child it stopped said on the way out.
public struct CLIClient: KeryxClient {
    static let migrationMarker = "keryx cannot do that yet:"
    static let restartReason = "from the Mac app"

    private let location: KeryxLocation
    private let runner: CommandRunner
    private let files: FileSystem

    public init(location: KeryxLocation, runner: CommandRunner = ProcessRunner(),
                files: FileSystem = LocalFileSystem()) {
        self.location = location
        self.runner = runner
        self.files = files
    }

    // MARK: - Reads

    public func status() async throws -> StatusReport {
        try await decode(["status", "--json"])
    }

    public func doctor() async throws -> DoctorReport {
        // Exit 1 is doctor saying something is not ok, with the report still on stdout.
        try await decode(["doctor", "--json"], acceptedExitCodes: [0, 1], timeout: .seconds(60))
    }

    public func tasks(includeInternal: Bool, limit: Int) async throws -> [TaskItem] {
        var arguments = ["tasks", "list", "--json", "--status", "all", "--limit", String(limit)]
        if includeInternal { arguments.append("--internal") }
        let list: TaskList = try await decode(arguments)
        return list.tasks
    }

    public func task(id: Int) async throws -> TaskDetail {
        try await decode(["tasks", "show", String(id), "--json"])
    }

    public func usage(days: Int) async throws -> UsageReport {
        try await decode(["tasks", "usage", "--json", "--days", String(days)])
    }

    public func models() async throws -> ModelsReport {
        try await decode(["models", "list", "--json"])
    }

    public func config() async throws -> [ConfigRow] {
        try await decode(["config", "list", "--json"])
    }

    public func options(key: String) async throws -> ConfigOptions {
        // A list asked of a provider can take a while; past that, the CLI falls back itself.
        try await decode(["config", "options", key, "--json"], timeout: .seconds(45))
    }

    public func approvals(limit: Int) async throws -> ApprovalsReport {
        try await decode(["approvals", "--json", "--limit", String(limit)])
    }

    public func memory() async throws -> MemoryDocument {
        try await decode(["memory", "--json"])
    }

    // MARK: - Models

    /// Ends normally only after a terminal event (done, adopted, error, cancelled). A stream
    /// that stops short of one (the CLI crashed, was killed, or printed nothing) throws,
    /// because "it ended" is not "it finished". `stop()` stops the CLI's process group
    /// (which it answers with `cancelled`), and the stream ends once the CLI has exited.
    public func pull(model: String) -> PullStream {
        events(["models", "pull", model, "--json"], parse: PullEvent.parse,
               concludes: { if case .progress = $0 { false } else { true } },
               stoppedWithoutSaying: PullEvent.stoppedWithoutSaying)
    }

    public func use(model: String, defaultAgent: Bool) async throws -> UseResult {
        var arguments = ["models", "use", model]
        if defaultAgent { arguments.append("--default-agent") }
        arguments.append("--json")
        return try await decode(arguments)
    }

    // MARK: - Writes

    public func set(key: String, value: String) async throws {
        // A value that starts with "-" would be read as an option without the "--".
        let pair = value.hasPrefix("-") ? ["--", key, value] : [key, value]
        _ = try await execute(request(["config", "set"] + pair))
    }

    public func setSecret(key: String, value: String) async throws {
        _ = try await execute(request(["config", "set", key, "--stdin"], stdin: Data(value.utf8)))
    }

    public func unset(key: String) async throws {
        try await unset(keys: [key])
    }

    /// Every key in one command, after `--` so none is read as an option.
    public func unset(keys: [String]) async throws {
        guard !keys.isEmpty else { return }
        _ = try await execute(request(["config", "unset"] + (keys.contains { $0.hasPrefix("-") } ? ["--"] : []) + keys))
    }

    public func setEscalation(enabled: Bool) async throws {
        _ = try await execute(request(["approvals", enabled ? "--enable" : "--disable"]))
    }

    public func restart(force: Bool) async throws {
        var arguments = ["restart", "--reason", Self.restartReason]
        if force { arguments.append("--force") }
        let result = try await run(request(arguments, timeout: .seconds(60)))
        if result.exitCode == 1, let count = Self.liveSessions(in: result.stdoutText) {
            throw KeryxError.liveCalls(count)
        }
        try check(result, command: Self.name(arguments))
    }

    // MARK: - Voice

    public func voice() async throws -> VoiceStatus {
        try await decode(["voice", "--json"])
    }

    public func useVoiceOpenAI() async throws -> VoiceUseResult {
        try await decode(["voice", "use", "openai", "--json"])
    }

    /// Exit 1 with a `problem` is the handshake failing, and is decoded; exit 2 (a refusal)
    /// and anything unreadable are failures. `--key-stdin` only with a key that is not
    /// blank, and never with `--clear-key`: the CLI refuses both.
    public func useVoiceServer(url: String, key: ServerKey, keepAnyway: Bool) async throws -> VoiceServerResult {
        var flags: [String] = []
        let sent = key.toSend
        if sent != nil { flags.append("--key-stdin") }
        if key == .clear { flags.append("--clear-key") }
        if keepAnyway { flags.append("--keep-anyway") }
        flags.append("--json")
        // An address that starts with "-" would be read as an option without the "--".
        let arguments = url.hasPrefix("-")
            ? ["voice", "use", "server"] + flags + ["--", url]
            : ["voice", "use", "server", url] + flags
        return try await decode(arguments, acceptedExitCodes: [0, 1], timeout: .seconds(60),
                                stdin: sent.map { Data($0.utf8) })
    }

    /// Ends normally only after `done`, `error` or `cancelled`, as `pull` does. The first
    /// start can take minutes, so there is no timeout.
    public func voiceHere(voice: String?) -> VoiceSetupStream {
        var arguments = ["voice", "use", "here"]
        if let voice { arguments += ["--voice", voice] }
        arguments.append("--json")
        return events(arguments, parse: VoiceSetupEvent.parse, concludes: { $0.isTerminal },
                      stoppedWithoutSaying: VoiceSetupEvent.stoppedWithoutSaying)
    }

    // MARK: - Setup

    public func setupPlan() async throws -> SetupPlan {
        // It asks what doctor asks, Twilio included.
        try await decode(["setup", "plan", "--json"], timeout: .seconds(60))
    }

    public func markSetupStep(_ step: String) async throws -> SetupMarkResult {
        try await decode(["setup", "mark", step, "--json"])
    }

    public func pin() async throws -> PinStatus {
        try await decode(["pin", "--json"])
    }

    /// The PIN twice on stdin, one per line, as `pin set --stdin` reads it; never argv. A
    /// refusal (already set, in the environment, two that differ) is the CLI's stderr.
    public func setPin(_ pin: String, replace: Bool) async throws -> PinSetResult {
        // A break would make the two lines the CLI compares something else.
        guard !pin.contains(where: \.isNewline) else { throw KeryxError.refused(Self.pinOnOneLine) }
        var arguments = ["pin", "set", "--stdin"]
        if replace { arguments.append("--replace") }
        arguments.append("--json")
        return try await decode(arguments, stdin: Data("\(pin)\n\(pin)\n".utf8))
    }

    public func verify(key: String, value: String) async throws -> VerifyResult {
        try await decode(["config", "verify", key, "--stdin", "--json"],
                         stdin: Data((try Self.oneLine(value, "\(key)") + "\n").utf8))
    }

    public func verifyTwilio(accountSID: String, authToken: String) async throws -> VerifyResult {
        let lines = try Self.oneLine(accountSID, "The account SID") + "\n"
            + Self.oneLine(authToken, "The auth token") + "\n"
        return try await decode(["config", "verify", "TWILIO_AUTH_TOKEN", "--stdin", "--pair", "--json"],
                                stdin: Data(lines.utf8))
    }

    // MARK: - Setup sections

    public func profileQuestions() async throws -> ProfileQuestions {
        try await decode(["setup", "profile", "--json"])
    }

    public func previewProfile(_ answers: ProfileAnswers) async throws -> ProfileResult {
        try await decode(["setup", "profile", "--preview", "--json"], stdin: try answers.json())
    }

    /// Exit 1 with a document is a refusal (`status` `refused`); without one, a failure.
    public func applyProfile(_ answers: ProfileAnswers) async throws -> ProfileResult {
        try await decode(["setup", "profile", "--apply", "--json"], acceptedExitCodes: [0, 1],
                         stdin: try answers.json())
    }

    public func migrationPlan() async throws -> MigrationPlan {
        try await decode(["migrate", "--json"], timeout: .seconds(60))
    }

    /// No timeout: stopping the service first waits up to half a minute, and moving a large
    /// install can take longer.
    public func migrate() -> MigrationStream {
        events(["migrate", "--yes", "--json"])
    }

    public func projectContextQuestion() async throws -> ProjectContextQuestion {
        try await decode(["setup", "project-context", "--describe", "--json"])
    }

    /// Each folder as `--folder=PATH`, so a path that starts with "-" is still its value.
    public func draftProjectContext(_ scope: ProjectScope) -> ProjectDraftStream {
        var arguments = ["setup", "project-context", "--json"]
        switch scope {
        case .explore: arguments.append("--explore")
        case let .folders(paths): arguments += paths.map { "--folder=" + $0 }
        }
        return events(arguments)
    }

    /// Exit 1 with a document is a problem in the draft; without one, a failure.
    public func applyProjectContext(_ keep: ProjectContextKeep) async throws -> ProjectContextResult {
        try await decode(["setup", "project-context", "--apply", "--json"], acceptedExitCodes: [0, 1],
                         stdin: try keep.json())
    }

    // MARK: - Phone

    public func phoneNumbers() async throws -> PhoneNumbers {
        try await decode(["phone", "numbers", "--json"])
    }

    public func phoneWebhooks() async throws -> PhoneWebhooks {
        try await decode(["phone", "webhooks", "--json"])
    }

    public func applyPhoneWebhooks() async throws -> WebhooksApplied {
        try await decode(["phone", "webhooks", "--apply", "--json"], timeout: .seconds(60))
    }

    public func phoneTunnel() async throws -> PhoneTunnel {
        try await decode(["phone", "tunnel", "--json"])
    }

    /// Homebrew's own places, after the PATH Keryx runs with.
    static let homebrewPlaces = ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]

    public func homebrew() -> String? {
        let path = (location.environment["PATH"] ?? "").split(separator: ":").map { String($0) + "/brew" }
        return (path + Self.homebrewPlaces).first { files.isExecutableFile(atPath: $0) }
    }

    /// Run as the owner's Homebrew, in `/`, with Keryx's own environment (no actor
    /// variables); never a shell. No timeout: a first install fetches its bottles.
    public func installCloudflared(brew: String) -> StoppableStream<String> {
        let request = CommandRequest(executable: brew, arguments: ["install", "cloudflared"],
                                     environment: Sanitise.environment(location.environment)
                                         .merging(["HOMEBREW_NO_ENV_HINTS": "1"]) { $1 },
                                     workingDirectory: "/", stdin: nil, timeout: nil)
        let lines = runner.lines(request)
        let stream = AsyncThrowingStream<String, Error> { continuation in
            let task = Task {
                do {
                    for try await line in lines {
                        let trimmed = line.trimmingCharacters(in: .whitespaces)
                        if !trimmed.isEmpty { continuation.yield(trimmed) }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: Self.map(error, command: "brew install"))
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
        return StoppableStream(stream, stop: { lines.stop() })
    }

    // MARK: - Service

    public func service() async throws -> ServiceReport {
        try await decode(["service", "--json"])
    }

    public func installService() async throws -> ServiceResult {
        try await decode(["service", "install", "--json"], timeout: Self.installerTimeout)
    }

    public func uninstallService() async throws -> ServiceResult {
        try await decode(["service", "uninstall", "--json"], timeout: Self.installerTimeout)
    }

    /// Ends normally only after `done`, `error` or `cancelled`, as `pull` does; no timeout,
    /// since installing llama.cpp and loading a model can take minutes.
    public func startModelServer(runtime: String?) -> ModelServerStream {
        var arguments = ["models", "start"]
        if let runtime { arguments.append("--runtime=" + runtime) }
        arguments.append("--json")
        return events(arguments)
    }

    public func stopModelServer() async throws -> ServiceResult {
        try await decode(["models", "stop", "--json"], timeout: Self.installerTimeout)
    }

    public func downloadVoiceModels() -> VoiceDownloadStream {
        events(["voice", "download", "--json"])
    }

    // MARK: - Accounts

    /// The child's stdin stays open for the line a sign-in asks for (`send`); the client
    /// file is named with `=`, so a path that starts with "-" is still its value.
    public func signIn(_ provider: SignInProvider, clientFile: String?) -> SignInStream {
        var arguments = ["auth", "login", provider.rawValue]
        if let clientFile { arguments.append("--client-file=" + clientFile) }
        arguments.append("--json")
        return events(arguments, interactive: true)
    }

    public func signOut(_ provider: SignInProvider) async throws -> SignOutResult {
        try await decode(["auth", "logout", provider.rawValue, "--json"])
    }

    public func integrations() async throws -> IntegrationsReport {
        try await decode(["integrations", "--json"])
    }

    // MARK: - Plugins

    public func plugins() async throws -> PluginsReport {
        try await decode(["plugins", "list", "--json"])
    }

    /// Each value as `--set=KEY=VALUE` (and `--cluster=`, `--guard=`), so one that starts
    /// with "-" is still a value; in key order, so the same values make the same command.
    public func installPlugin(_ name: String, values: PluginValues) async throws -> PluginResult {
        var arguments = ["plugins", "install", name]
        arguments += Self.pairs(values.settings).map { "--set=" + $0 }
        arguments += Self.pairs(values.clusters).map { "--cluster=" + $0 }
        if let guardScript = values.guardScript { arguments.append("--guard=" + guardScript) }
        arguments.append("--json")
        return try await decode(arguments)
    }

    public func removePlugin(_ name: String) async throws -> PluginResult {
        try await decode(["plugins", "remove", name, "--json"])
    }

    /// `KEY=VALUE` after `--`, so none is read as an option whatever it starts with.
    public func setPlugin(_ name: String, values: PluginValues) async throws -> PluginResult {
        var arguments = ["plugins", "set", name]
        arguments += Self.pairs(values.clusters).map { "--cluster=" + $0 }
        arguments.append("--json")
        let assignments = Self.pairs(values.settings)
        if !assignments.isEmpty { arguments += ["--"] + assignments }
        return try await decode(arguments)
    }

    /// Exit 1 is a file the next call would refuse, with the report still printed.
    public func tools() async throws -> ToolsReport {
        try await decode(["tools", "--json"], acceptedExitCodes: [0, 1])
    }

    public func sshHosts() async throws -> SshHostsReport {
        try await decode(["plugins", "hosts", "--json"])
    }

    /// `KEY=VALUE` for each of `values`, in key order.
    static func pairs(_ values: [String: String]) -> [String] {
        values.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }
    }

    /// `text` trimmed, when it is one line: what goes on stdin is. Text with a line break
    /// inside it is refused (`KeryxError.refused`, naming `what`), never cut to its first
    /// line — that would check something the owner did not type.
    static func oneLine(_ text: String, _ what: String) throws -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.contains(where: \.isNewline) else {
            throw KeryxError.refused("\(what) has a line break in it; paste it again as one line.")
        }
        return trimmed
    }

    /// What a PIN with a line break in it says.
    static let pinOnOneLine = "The PIN is digits on one line; type it again."

    /// The checkout's installers start and stop launchd units.
    static let installerTimeout: Duration = .seconds(300)

    // MARK: - Streams

    /// A `StreamEvent` stream: `events` with its parse, its endings and its words.
    private func events<Done: StreamDone>(
        _ subcommand: [String], interactive: Bool = false
    ) -> StoppableStream<StreamEvent<Done>> {
        events(subcommand, parse: StreamEvent<Done>.parse, concludes: { $0.isTerminal },
               stoppedWithoutSaying: Done.stoppedWithoutSaying, interactive: interactive)
    }

    /// `subcommand`'s stdout as events, which ends normally only once one that `concludes`
    /// has come and the CLI has exited, and otherwise throws `stoppedWithoutSaying`. Lines
    /// `parse` does not know are skipped. `stop()` stops the CLI's process group.
    /// `interactive`: the child's stdin stays open, and the stream's `send` writes to it.
    private func events<Event: Sendable>(
        _ subcommand: [String], parse: @escaping @Sendable (String) -> Event?,
        concludes: @escaping @Sendable (Event) -> Bool, stoppedWithoutSaying: String,
        interactive: Bool = false
    ) -> StoppableStream<Event> {
        let command = Self.name(subcommand)
        let request = request(subcommand, timeout: nil)
        let lines = interactive ? runner.interactiveLines(request) : runner.lines(request)
        let events = AsyncThrowingStream<Event, Error> { continuation in
            let task = Task {
                do {
                    var concluded = false
                    for try await line in lines {
                        guard let event = parse(line) else { continue }
                        if concludes(event) { concluded = true }
                        continuation.yield(event)
                    }
                    if concluded {
                        continuation.finish()
                    } else {
                        continuation.finish(throwing: KeryxError.failed(
                            command: command, exitCode: -1, message: stoppedWithoutSaying))
                    }
                } catch {
                    continuation.finish(throwing: Self.map(error, command: command))
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
        return StoppableStream(events, stop: { lines.stop() }, input: lines.input)
    }

    // MARK: - Running

    private func request(
        _ subcommand: [String], stdin: Data? = nil, timeout: Duration? = .seconds(30)
    ) -> CommandRequest {
        CommandRequest(
            executable: location.executable,
            arguments: location.prefixArguments + subcommand,
            environment: Sanitise.environment(location.environment),
            workingDirectory: location.runDirectory,
            stdin: stdin,
            timeout: timeout)
    }

    private func decode<T: Decodable>(
        _ subcommand: [String], acceptedExitCodes: Set<Int32> = [0], timeout: Duration = .seconds(30),
        stdin: Data? = nil
    ) async throws -> T {
        let command = Self.name(subcommand)
        let result = try await run(request(subcommand, stdin: stdin, timeout: timeout), command: command)
        if !acceptedExitCodes.contains(result.exitCode) {
            try check(result, command: command) // throws: every exit but 0 is a failure
        }
        do {
            return try JSON.decoder.decode(T.self, from: result.stdout)
        } catch {
            // An accepted non-zero exit with nothing readable is the failure it looks like.
            if result.exitCode != 0 { throw Self.failure(result, command: command) }
            throw KeryxError.decoding(command: command, detail: Self.describe(error))
        }
    }

    private func execute(_ request: CommandRequest, command: String? = nil) async throws -> CommandResult {
        let command = command ?? name(of: request)
        let result = try await run(request, command: command)
        try check(result, command: command)
        return result
    }

    /// The runner, with its own errors turned into `KeryxError`s. Cancelled — before the
    /// child starts, or while it runs — it throws `CancellationError`: the child the runner
    /// stopped exits 15 (or times out), and that is not the command failing.
    private func run(_ request: CommandRequest, command: String? = nil) async throws -> CommandResult {
        let command = command ?? name(of: request)
        try Task.checkCancellation()
        let result: CommandResult
        do {
            result = try await runner.run(request)
        } catch {
            try Task.checkCancellation()
            throw Self.map(error, command: command)
        }
        try Task.checkCancellation()
        return result
    }

    private func name(of request: CommandRequest) -> String {
        Self.name(Array(request.arguments.dropFirst(location.prefixArguments.count)))
    }

    private func check(_ result: CommandResult, command: String) throws {
        guard result.exitCode != 0 else { return }
        if result.exitCode == 2, let sentence = Self.migrationSentence(in: result.stderrText) {
            throw KeryxError.migrationRequired(sentence)
        }
        throw Self.failure(result, command: command)
    }

    // MARK: - Mapping

    static func map(_ error: Error, command: String) -> Error {
        switch error {
        case RunnerError.timedOut: KeryxError.timedOut(command: command)
        case let RunnerError.launchFailed(message): KeryxError.launch(message)
        default: error
        }
    }

    static func failure(_ result: CommandResult, command: String) -> KeryxError {
        let stderr = result.stderrText.trimmingCharacters(in: .whitespacesAndNewlines)
        let message = stderr.isEmpty
            ? result.stdoutText.trimmingCharacters(in: .whitespacesAndNewlines) : stderr
        return .failed(command: command, exitCode: result.exitCode, message: message)
    }

    /// `keryx` and up to two subcommand words — never an option, a key's value or an id
    /// past them, so nothing sensitive reaches an error message by way of the name.
    static func name(_ subcommand: [String]) -> String {
        (["keryx"] + subcommand.prefix { !$0.hasPrefix("-") }.prefix(2)).joined(separator: " ")
    }

    static func migrationSentence(in stderr: String) -> String? {
        for line in stderr.split(whereSeparator: \.isNewline) {
            if let range = line.range(of: migrationMarker) {
                return line[range.upperBound...].trimmingCharacters(in: .whitespaces)
            }
        }
        return nil
    }

    static func liveSessions(in stdout: String) -> Int? {
        guard let regex = try? Regex(#"(\d+) live session"#),
              let match = stdout.firstMatch(of: regex),
              let digits = match.output[1].substring
        else { return nil }
        return Int(digits)
    }

    static func describe(_ error: Error) -> String {
        switch error {
        case let DecodingError.keyNotFound(key, context):
            "no \(key.stringValue) at \(path(context))"
        case let DecodingError.typeMismatch(_, context), let DecodingError.valueNotFound(_, context):
            "\(context.debugDescription) at \(path(context))"
        case let DecodingError.dataCorrupted(context):
            context.debugDescription
        default:
            String(describing: error)
        }
    }

    private static func path(_ context: DecodingError.Context) -> String {
        let path = context.codingPath.map(\.stringValue).joined(separator: ".")
        return path.isEmpty ? "the top level" : path
    }
}
