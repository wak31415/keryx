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

    public init(location: KeryxLocation, runner: CommandRunner = ProcessRunner()) {
        self.location = location
        self.runner = runner
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

    public func installModelServer() async throws -> String {
        guard let checkout = location.checkout else {
            throw KeryxError.launch(
                "Starting the model server from the app needs a Keryx checkout. Run keryx setup in Terminal instead.")
        }
        let request = CommandRequest(
            executable: checkout + "/scripts/install-launchd.sh", arguments: ["--llm"],
            environment: Sanitise.environment(location.environment),
            workingDirectory: checkout, timeout: .seconds(300))
        let result = try await execute(request, command: "install-launchd.sh --llm")
        return result.stdoutText
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
        _ = try await execute(request(["config", "unset", key]))
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

    // MARK: - Streams

    /// `subcommand`'s stdout as events, which ends normally only once one that `concludes`
    /// has come and the CLI has exited, and otherwise throws `stoppedWithoutSaying`. Lines
    /// `parse` does not know are skipped. `stop()` stops the CLI's process group.
    private func events<Event: Sendable>(
        _ subcommand: [String], parse: @escaping @Sendable (String) -> Event?,
        concludes: @escaping @Sendable (Event) -> Bool, stoppedWithoutSaying: String
    ) -> StoppableStream<Event> {
        let command = Self.name(subcommand)
        let lines = runner.lines(request(subcommand, timeout: nil))
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
        return StoppableStream(events, stop: { lines.stop() })
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
