import Foundation
import Testing
@testable import KeryxKit
import KeryxTestSupport
import KeryxPreview

@Suite struct CLIClientTests {
    static func ok(_ stdout: Data = Data()) -> CommandResult {
        .init(exitCode: 0, stdout: stdout, stderr: Data())
    }

    // MARK: - Secrets and the environment

    @Test func setSecretUsesStdinNotArgv() async throws {
        let fake = FakeRunner(); fake.responder = { _ in .init(exitCode: 0, stdout: Data(), stderr: Data()) }
        let client = CLIClient(location: .fixture, runner: fake)
        try await client.setSecret(key: "OPENAI_API_KEY", value: "sk-very-secret")
        let request = try #require(fake.requests.last)
        #expect(request.arguments.suffix(4) == ["config", "set", "OPENAI_API_KEY", "--stdin"])
        #expect(!request.arguments.joined().contains("sk-very-secret"))
        #expect(!request.environment.values.contains { $0.contains("sk-very-secret") })
        #expect(request.stdin == Data("sk-very-secret".utf8))
    }

    @Test func everyRequestRunsWhereTheLocationSays() async throws {
        var location = KeryxLocation.fixture
        location.environment["KERYX_ACTOR"] = "service"
        location.environment["JARVIS_ACTOR"] = "service"
        let fake = FakeRunner { _ in Self.ok(PreviewData.data("status.json")) }
        let client = CLIClient(location: location, runner: fake)
        _ = try await client.status()
        let request = try #require(fake.requests.last)
        #expect(request.executable == location.executable)
        #expect(request.arguments == location.prefixArguments + ["status", "--json"])
        #expect(request.workingDirectory == location.checkout)
        #expect(request.environment["KERYX_ACTOR"] == nil)
        #expect(request.environment["JARVIS_ACTOR"] == nil)
        #expect(request.environment["NO_COLOR"] == "1")
        #expect(request.environment["KERYX_HOME"] == location.environment["KERYX_HOME"])
        #expect(request.stdin == nil)
    }

    @Test func withNoCheckoutEveryCommandRunsFromTheRoot() async throws {
        var location = KeryxLocation.fixture
        location.checkout = nil
        location.executable = "/Users/you/.local/bin/keryx"
        location.prefixArguments = []
        location.workingDirectory = "/Users/you"   // never HOME, whatever the location says
        let fake = FakeRunner { _ in Self.ok(PreviewData.data("status.json")) }
        let client = CLIClient(location: location, runner: fake)
        _ = try await client.status()
        try await client.set(key: "ASSISTANT_NAME", value: "Vega")
        #expect(fake.requests.count == 2)
        #expect(fake.requests.allSatisfy { $0.workingDirectory == "/" })
    }

    // MARK: - Cancellation

    /// A runner that runs until its caller is cancelled, then answers as a child stopped by
    /// SIGTERM does: exit 15, a word on stderr.
    final class StoppedBySignal: CommandRunner, @unchecked Sendable {
        private let lock = NSLock()
        private var started = 0
        var runs: Int { lock.withLock { started } }

        func run(_ request: CommandRequest) async throws -> CommandResult {
            lock.withLock { started += 1 }
            while !Task.isCancelled { await Task.yield() }
            return .init(exitCode: 15, stdout: Data(), stderr: Data("Terminated\n".utf8))
        }

        func lines(_ request: CommandRequest) -> LineStream { LineStream(AsyncThrowingStream { $0.finish() }) }
    }

    @Test func aCancelledReadThrowsCancellationNotAFailure() async throws {
        let runner = StoppedBySignal()
        let client = CLIClient(location: .fixture, runner: runner)
        let read = Task { try await client.status() }
        while runner.runs == 0 { await Task.yield() }
        read.cancel()
        await #expect(throws: CancellationError.self) { _ = try await read.value }

        let write = Task { try await client.unset(key: "ASSISTANT_NAME") }
        while runner.runs == 1 { await Task.yield() }
        write.cancel()
        await #expect(throws: CancellationError.self) { try await write.value }
    }

    @Test func aReadCancelledBeforeItStartsRunsNothing() async throws {
        let fake = FakeRunner { _ in Self.ok(PreviewData.data("status.json")) }
        let client = CLIClient(location: .fixture, runner: fake)
        let read = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try await client.status()
        }
        await #expect(throws: CancellationError.self) { _ = try await read.value }
        #expect(fake.requests.isEmpty)
    }

    @Test func setPassesKeyAndValue() async throws {
        let fake = FakeRunner()
        let client = CLIClient(location: .fixture, runner: fake)
        try await client.set(key: "ASSISTANT_NAME", value: "Lyra")
        #expect(fake.requests.last?.arguments.suffix(4) == ["config", "set", "ASSISTANT_NAME", "Lyra"])
        try await client.set(key: "SOME_NUMBER", value: "-1")
        #expect(fake.requests.last?.arguments.suffix(5) == ["config", "set", "--", "SOME_NUMBER", "-1"])
    }

    // MARK: - Commands

    @Test func commandLines() async throws {
        let fake = FakeRunner { request in
            let words = Array(request.arguments.dropFirst(KeryxLocation.fixture.prefixArguments.count))
            switch words.first {
            case "doctor": return Self.ok(PreviewData.data("doctor.json"))
            case "tasks" where words[1] == "list": return Self.ok(PreviewData.data("tasks.json"))
            case "tasks" where words[1] == "show": return Self.ok(PreviewData.data("task-detail.json"))
            case "tasks": return Self.ok(PreviewData.data("usage.json"))
            case "models" where words[1] == "list": return Self.ok(PreviewData.data("models.json"))
            case "models":
                return Self.ok(Data(#"{"saved": {"LOCAL_AGENT_MODEL": "m"}, "llama_server": null, "restart_needed": true}"#.utf8))
            case "config" where words[1] == "list": return Self.ok(PreviewData.data("config.json"))
            case "approvals" where words.contains("--json"): return Self.ok(PreviewData.data("approvals.json"))
            case "memory": return Self.ok(PreviewData.data("memory.json"))
            default: return Self.ok()
            }
        }
        let client = CLIClient(location: .fixture, runner: fake)
        func last() -> [String] {
            Array(fake.requests.last!.arguments.dropFirst(KeryxLocation.fixture.prefixArguments.count))
        }

        _ = try await client.doctor()
        #expect(last() == ["doctor", "--json"])
        _ = try await client.tasks(includeInternal: false, limit: 50)
        #expect(last() == ["tasks", "list", "--json", "--status", "all", "--limit", "50"])
        let tasks = try await client.tasks(includeInternal: true, limit: 7)
        #expect(last() == ["tasks", "list", "--json", "--status", "all", "--limit", "7", "--internal"])
        #expect(tasks == PreviewData.tasks.tasks)
        _ = try await client.task(id: 42)
        #expect(last() == ["tasks", "show", "42", "--json"])
        _ = try await client.usage(days: 30)
        #expect(last() == ["tasks", "usage", "--json", "--days", "30"])
        _ = try await client.models()
        #expect(last() == ["models", "list", "--json"])
        let used = try await client.use(model: "m", defaultAgent: true)
        #expect(last() == ["models", "use", "m", "--default-agent", "--json"])
        #expect(used.restartNeeded)
        _ = try await client.use(model: "m", defaultAgent: false)
        #expect(last() == ["models", "use", "m", "--json"])
        let rows = try await client.config()
        #expect(last() == ["config", "list", "--json"])
        #expect(rows == PreviewData.config)
        try await client.unset(key: "OPENAI_VOICE")
        #expect(last() == ["config", "unset", "OPENAI_VOICE"])
        try await client.unset(keys: ["GOOGLE_WORKSPACE_MCP", "USER_GOOGLE_EMAIL"])
        #expect(last() == ["config", "unset", "GOOGLE_WORKSPACE_MCP", "USER_GOOGLE_EMAIL"], "one command for both")
        _ = try await client.approvals(limit: 20)
        #expect(last() == ["approvals", "--json", "--limit", "20"])
        try await client.setEscalation(enabled: false)
        #expect(last() == ["approvals", "--disable"])
        try await client.setEscalation(enabled: true)
        #expect(last() == ["approvals", "--enable"])
        _ = try await client.memory()
        #expect(last() == ["memory", "--json"])
        try await client.restart(force: false)
        #expect(last() == ["restart", "--reason", "from the Mac app"])
        #expect(fake.requests.last?.timeout == .seconds(60))
        try await client.restart(force: true)
        #expect(last() == ["restart", "--reason", "from the Mac app", "--force"])
    }

    @Test func doctorDecodesOnExitOne() async throws {
        let fake = FakeRunner { _ in .init(exitCode: 1, stdout: PreviewData.data("doctor.json"), stderr: Data()) }
        let client = CLIClient(location: .fixture, runner: fake)
        let report = try await client.doctor()
        #expect(report.ok == false)
        #expect(report == PreviewData.doctor)
    }

    @Test func doctorExitOneWithoutAReportIsAFailure() async throws {
        let fake = FakeRunner { _ in .init(exitCode: 1, stdout: Data(), stderr: Data("boom\n".utf8)) }
        let client = CLIClient(location: .fixture, runner: fake)
        await #expect(throws: KeryxError.failed(command: "keryx doctor", exitCode: 1, message: "boom")) {
            try await client.doctor()
        }
    }

    // MARK: - Errors

    @Test func migrationRequiredIsClassified() async throws {
        let sentence = "/Users/x/.config/jarvis still holds … — run `keryx migrate`"
        let fake = FakeRunner { _ in
            .init(exitCode: 2, stdout: Data(), stderr: Data("keryx cannot do that yet: \(sentence)\n".utf8))
        }
        let client = CLIClient(location: .fixture, runner: fake)
        await #expect(throws: KeryxError.migrationRequired(sentence)) {
            try await client.tasks(includeInternal: false, limit: 50)
        }
    }

    @Test func restartRefusedForLiveCalls() async throws {
        let fake = FakeRunner { _ in
            .init(exitCode: 1,
                  stdout: Data("2 live session(s): a restart cuts them off. Wait, or pass --force.\n".utf8),
                  stderr: Data())
        }
        let client = CLIClient(location: .fixture, runner: fake)
        await #expect(throws: KeryxError.liveCalls(2)) { try await client.restart(force: false) }
    }

    @Test func otherFailuresCarryStderrElseStdout() async throws {
        let fake = FakeRunner { _ in .init(exitCode: 1, stdout: Data("on stdout\n".utf8), stderr: Data()) }
        let client = CLIClient(location: .fixture, runner: fake)
        await #expect(throws: KeryxError.failed(command: "keryx restart", exitCode: 1, message: "on stdout")) {
            try await client.restart(force: true)
        }
        fake.responder = { _ in .init(exitCode: 3, stdout: Data("out".utf8), stderr: Data("  refused\n".utf8)) }
        await #expect(throws: KeryxError.failed(command: "keryx config set", exitCode: 3, message: "refused")) {
            try await client.set(key: "A", value: "b")
        }
    }

    @Test func runnerErrorsAreMapped() async throws {
        let fake = FakeRunner { _ in throw RunnerError.timedOut }
        let client = CLIClient(location: .fixture, runner: fake)
        await #expect(throws: KeryxError.timedOut(command: "keryx status")) { try await client.status() }
        fake.responder = { _ in throw RunnerError.launchFailed("no such file") }
        await #expect(throws: KeryxError.launch("no such file")) { try await client.status() }
    }

    @Test func undecodableOutputIsADecodingError() async throws {
        let fake = FakeRunner { _ in Self.ok(Data("not json".utf8)) }
        let client = CLIClient(location: .fixture, runner: fake)
        do {
            _ = try await client.memory()
            Issue.record("expected a decoding error")
        } catch let KeryxError.decoding(command, _) {
            #expect(command == "keryx memory")
        }
    }

    @Test func errorsHaveWords() {
        let errors: [KeryxError] = [
            .notFound(LocateFailure(lookedAt: ["a."])), .migrationRequired("x"), .liveCalls(1),
            .failed(command: "keryx status", exitCode: 1, message: ""),
            .decoding(command: "keryx status", detail: "d"), .timedOut(command: "keryx status"),
            .launch("l"), .terminal("t"), .refused("r"),
        ]
        for error in errors {
            #expect(!error.title.isEmpty)
            #expect(!error.message.isEmpty)
        }
        #expect(KeryxError.liveCalls(1).message.contains("1 call"))
        #expect(KeryxError.liveCalls(2).message.contains("2 calls"))
    }

    @Test func errorsSayWhatHappenedInSentences() {
        let errors: [KeryxError] = [
            .notFound(LocateFailure(lookedAt: ["no folder chosen"])), .migrationRequired("run keryx migrate"),
            .liveCalls(2), .failed(command: "keryx status", exitCode: 1, message: "database is locked"),
            .failed(command: "keryx status", exitCode: 3, message: ""),
            .decoding(command: "keryx status", detail: "d"), .timedOut(command: "keryx status"),
            .launch("no such file"), .terminal("Terminal could not be opened"),
            .refused(TrustedLink.refusal),
        ]
        for error in errors {
            #expect(error.message.hasSuffix("."), "\(error.message)")
            #expect(!error.title.hasSuffix("."), "\(error.title)")
        }
        #expect(KeryxError.launch("no such file").message == "No such file.")
        let refused = KeryxError.failed(command: "keryx tasks list", exitCode: 1, message: "database is locked")
        #expect(refused.message == "Keryx reported: “database is locked”.")
        #expect(refused.reported == "database is locked")
        #expect(refused.title(loading: "Sessions") == "Couldn't Load Sessions")
        #expect(KeryxError.timedOut(command: "keryx status").title(loading: "Sessions") == "Keryx Didn't Respond")
        #expect(KeryxError.timedOut(command: "keryx status").reported == nil)
    }

    // MARK: - Pull

    @Test func pullMapsLinesAndIgnoresNoise() async throws {
        let fake = FakeRunner()
        fake.lineResponder = { _ in
            ["Downloading…", #"{"event": "progress", "done": 1, "total": 4}"#, "", #"{"event": "done", "path": "/m.gguf"}"#]
        }
        let client = CLIClient(location: .fixture, runner: fake)
        var events: [PullEvent] = []
        for try await event in client.pull(model: "m") { events.append(event) }
        #expect(events == [.progress(done: 1, total: 4), .done(path: "/m.gguf")])
        let request = try #require(fake.requests.last)
        #expect(Array(request.arguments.suffix(4)) == ["models", "pull", "m", "--json"])
        #expect(request.timeout == nil)
    }

    static let unfinished = KeryxError.failed(
        command: "keryx models pull", exitCode: -1,
        message: "The download stopped without saying why.")

    @Test func pullEndingWithOnlyProgressThrows() async throws {
        let fake = FakeRunner()
        fake.lineResponder = { _ in [#"{"event": "progress", "done": 1, "total": 4}"#] }
        let client = CLIClient(location: .fixture, runner: fake)
        var events: [PullEvent] = []
        await #expect(throws: Self.unfinished) {
            for try await event in client.pull(model: "m") { events.append(event) }
        }
        #expect(events == [.progress(done: 1, total: 4)])
    }

    @Test func pullEndingWithNothingThrows() async throws {
        let fake = FakeRunner()
        fake.lineResponder = { _ in [] }
        let client = CLIClient(location: .fixture, runner: fake)
        await #expect(throws: Self.unfinished) {
            for try await _ in client.pull(model: "m") {}
        }
    }

    @Test func pullEndingWithAnErrorEventFinishesNormally() async throws {
        let fake = FakeRunner()
        fake.lineResponder = { _ in [#"{"event": "error", "message": "disk full"}"#] }
        let client = CLIClient(location: .fixture, runner: fake)
        var events: [PullEvent] = []
        for try await event in client.pull(model: "m") { events.append(event) }
        #expect(events == [.error("disk full")])
    }
}
