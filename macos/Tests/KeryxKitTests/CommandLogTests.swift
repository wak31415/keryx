import Foundation
import Testing
@testable import KeryxKit
import KeryxTestSupport
import KeryxPreview

@Suite struct CommandLogTests {
    static let uv = "/Users/you/.local/bin/uv"
    static let prefix = KeryxLocation.fixture.prefixArguments

    /// What `arguments` (run as `keryx`) logs, which must be `expected` and hold none of
    /// `secrets`.
    static func check(_ arguments: [String], logs expected: String, hiding secrets: [String],
                      executable: String = "/Users/you/code/keryx/.venv/bin/keryx",
                      sourceLocation: SourceLocation = #_sourceLocation) {
        let line = CommandLog.describe(executable: executable, arguments: arguments)
        #expect(line == expected, sourceLocation: sourceLocation)
        for secret in secrets {
            #expect(!line.contains(secret), "logged \(secret)", sourceLocation: sourceLocation)
        }
    }

    @Test func configSetLogsNeitherKeyNorValue() {
        Self.check(["config", "set", "OWNER_NUMBER", "+15551234567"],
                   logs: "keryx config set …", hiding: ["OWNER_NUMBER", "+1555", "1234567"])
    }

    @Test func aValueAfterTheSeparatorIsAValueWhateverItLooksLike() {
        Self.check(["config", "set", "--", "VOICE_PROMPT", "-value"],
                   logs: "keryx config set …", hiding: ["VOICE_PROMPT", "-value"])
        Self.check(["config", "set", "--", "SOME_KEY", "--json"],
                   logs: "keryx config set …", hiding: ["SOME_KEY", "--json"])
    }

    @Test func aSecretSetOnStdinLogsTheFlagButNotTheKey() {
        Self.check(["config", "set", "OPENAI_API_KEY", "--stdin"],
                   logs: "keryx config set … --stdin", hiding: ["OPENAI_API_KEY"])
    }

    @Test func configUnsetLogsNoKey() {
        Self.check(["config", "unset", "SLACK_BOT_TOKEN"],
                   logs: "keryx config unset …", hiding: ["SLACK_BOT_TOKEN"])
    }

    @Test func modelsPullLogsNoModelName() {
        Self.check(["models", "pull", "qwen3-coder:30b", "--json"],
                   logs: "keryx models pull … --json", hiding: ["qwen3", "30b"])
    }

    @Test func modelsUseLogsNoModelName() {
        Self.check(["models", "use", "gemma3", "--default-agent", "--json"],
                   logs: "keryx models use … --default-agent --json", hiding: ["gemma3"])
    }

    @Test func configOptionsLogsNoKey() {
        Self.check(["config", "options", "OPENAI_REALTIME_MODEL", "--json"],
                   logs: "keryx config options … --json", hiding: ["OPENAI_REALTIME_MODEL", "REALTIME"])
    }

    @Test func voiceAndItsModesAreLogged() {
        Self.check(["voice", "--json"], logs: "keryx voice --json", hiding: [])
        Self.check(["voice", "use", "openai", "--json"], logs: "keryx voice use openai --json", hiding: [])
    }

    @Test func voiceUseServerLogsNoAddressAndNoKey() {
        Self.check(["voice", "use", "server", "http://studio.local:8000/v1", "--key-stdin", "--keep-anyway", "--json"],
                   logs: "keryx voice use server … --key-stdin --keep-anyway --json",
                   hiding: ["studio", "8000", "http"])
        Self.check(["voice", "use", "server", "--json", "--", "-weird"],
                   logs: "keryx voice use server --json …", hiding: ["-weird"])
    }

    @Test func voiceUseHereLogsTheFlagButNotTheVoice() {
        Self.check(["voice", "use", "here", "--voice", "bf_emma", "--json"],
                   logs: "keryx voice use here --voice … --json", hiding: ["bf_emma", "emma"])
        // A value that looks like a mode is still a value past its place.
        Self.check(["voice", "use", "here", "--voice", "openai"],
                   logs: "keryx voice use here --voice …", hiding: ["openai"])
    }

    @Test func aModeIsOnlyAModeThirdAndAfterItsVerb() {
        Self.check(["voice", "here"], logs: "keryx voice …", hiding: ["here"])
        Self.check(["config", "set", "here"], logs: "keryx config set …", hiding: ["here"])
        Self.check(["voice", "use", "elsewhere"], logs: "keryx voice use …", hiding: ["elsewhere"])
    }

    @Test func tasksShowLogsNoTaskID() {
        Self.check(["tasks", "show", "12", "--json"],
                   logs: "keryx tasks show … --json", hiding: ["12"])
    }

    @Test func aListedFlagIsLoggedButNotItsValue() {
        Self.check(["tasks", "list", "--json", "--status", "all", "--limit", "200"],
                   logs: "keryx tasks list --json --status … --limit …", hiding: ["all", "200"])
    }

    @Test func restartLogsNoReason() {
        Self.check(["restart", "--reason", "from the Mac app", "--force"],
                   logs: "keryx restart … --force", hiding: ["--reason", "from the Mac app", "Mac"])
    }

    @Test func plainCommandsAreLoggedWhole() {
        Self.check(["status", "--json"], logs: "keryx status --json", hiding: [])
        Self.check(["doctor", "--json"], logs: "keryx doctor --json", hiding: [])
        Self.check(["approvals", "--disable"], logs: "keryx approvals --disable", hiding: [])
    }

    @Test func anUnknownSubcommandOrVerbIsAValue() {
        Self.check(["setup", "--all"], logs: "keryx …", hiding: ["setup", "--all"])
        Self.check(["config", "get", "PIN"], logs: "keryx config …", hiding: ["get", "PIN"])
        // A verb is only a verb second: a model named like one is still a value.
        Self.check(["models", "pull", "list"], logs: "keryx models pull …", hiding: [" list"])
    }

    @Test func aShellLogsNoScript() {
        Self.check(["-lic", "env; command -v keryx"], logs: "zsh …",
                   hiding: ["-lic", "env", "command"], executable: "/bin/zsh")
    }

    @Test func otherProgramsLogTheirNameAndNoUnlistedArgument() {
        Self.check(["--llm", "/Users/you/x"], logs: "install-launchd.sh --llm …", hiding: ["/Users/you"],
                   executable: "/Users/you/code/keryx/scripts/install-launchd.sh")
        // Another program's arguments are never read as keryx subcommands.
        Self.check(["status"], logs: "zsh …", hiding: ["status"], executable: "/bin/zsh")
    }

    @Test func thePrefixThatRunsKeryxIsDropped() {
        let line = CommandLog.describe(
            executable: Self.uv, arguments: Self.prefix + ["config", "set", "ASSISTANT_NAME", "Vega"],
            prefixCount: Self.prefix.count)
        #expect(line == "keryx config set …")
        for secret in ["uv", "run", "--project", "/Users/you", "--no-sync", "ASSISTANT_NAME", "Vega"] {
            #expect(!line.contains(secret), "logged \(secret)")
        }
    }

    @Test func theLoggingRunnerNamesKeryxOnlyPastItsPrefix() {
        let runner = LoggingRunner(FakeRunner(), prefix: Self.prefix)
        #expect(runner.describe(CommandRequest(executable: Self.uv, arguments: Self.prefix + ["status", "--json"]))
            == "keryx status --json")
        // The model server's installer, run by the same client: itself, not `keryx`.
        let installer = CommandRequest(executable: "/Users/you/code/keryx/scripts/install-launchd.sh",
                                       arguments: ["--llm"])
        #expect(runner.describe(installer) == "install-launchd.sh --llm")
        // A checkout's own venv keryx has no prefix, and is keryx by its name.
        let venv = LoggingRunner(FakeRunner())
        #expect(venv.describe(CommandRequest(executable: "/Users/you/code/keryx/.venv/bin/keryx",
                                             arguments: ["doctor", "--json"])) == "keryx doctor --json")
        #expect(venv.describe(installer) == "install-launchd.sh --llm")
    }

    @Test func errorsAreNamedByTheirCaseOnly() {
        #expect(CommandLog.errorName(RunnerError.launchFailed("/Users/you/.local/bin/uv: no such file"))
            == "launchFailed")
        #expect(CommandLog.errorName(RunnerError.timedOut) == "timedOut")
        #expect(CommandLog.errorName(RunnerError.stillRunning) == "stillRunning")
        #expect(CommandLog.errorName(CancellationError()) == "cancelled")
        #expect(CommandLog.errorName(KeryxError.launch("/Users/you/secret path")) == "KeryxError")
    }

    @Test func theLoggingRunnerPassesRequestsAndResultsThrough() async throws {
        let fake = FakeRunner(responder: { _ in .init(exitCode: 3, stdout: Data("out".utf8), stderr: Data()) })
        let runner = LoggingRunner(fake, prefix: Self.prefix)
        let request = CommandRequest(executable: Self.uv, arguments: Self.prefix + ["status", "--json"])
        let result = try await runner.run(request)
        #expect(result.exitCode == 3)
        #expect(result.stdoutText == "out")
        #expect(fake.requests == [request])
    }
}
