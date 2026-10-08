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

    @Test func clearingTheServerKeyIsLogged() {
        Self.check(["voice", "use", "server", "http://studio.local:8000/v1", "--clear-key", "--json"],
                   logs: "keryx voice use server … --clear-key --json", hiding: ["studio"])
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
        Self.check(["restore", "--dry-run"], logs: "keryx …", hiding: ["restore", "--dry-run"])
        Self.check(["migrate", "--dry-run"], logs: "keryx migrate …", hiding: ["--dry-run"])
        Self.check(["setup", "--mystery"], logs: "keryx setup …", hiding: ["--mystery"])
        Self.check(["config", "get", "PIN"], logs: "keryx config …", hiding: ["get", "PIN"])
        // A verb is only a verb second: a model named like one is still a value.
        Self.check(["models", "pull", "list"], logs: "keryx models pull …", hiding: [" list"])
    }

    // MARK: - Setup, the phone, the service, sign-ins and plugins

    @Test func setupLogsItsVerbsButNotTheStep() {
        Self.check(["setup", "plan", "--json"], logs: "keryx setup plan --json", hiding: [])
        Self.check(["setup", "mark", "pin", "--json"], logs: "keryx setup mark … --json", hiding: ["pin"])
    }

    @Test func thePinIsNeverOnTheCommandLineAndItsFlagsAreLogged() {
        Self.check(["pin", "--json"], logs: "keryx pin --json", hiding: [])
        Self.check(["pin", "set", "--stdin", "--replace", "--json"],
                   logs: "keryx pin set --stdin --replace --json", hiding: [])
    }

    @Test func verifyLogsItsFlagsButNotTheKey() {
        Self.check(["config", "verify", "TWILIO_AUTH_TOKEN", "--stdin", "--pair", "--json"],
                   logs: "keryx config verify … --stdin --pair --json", hiding: ["TWILIO", "TOKEN"])
    }

    @Test func thePhoneAndTheServiceAreLoggedWhole() {
        Self.check(["phone", "numbers", "--json"], logs: "keryx phone numbers --json", hiding: [])
        Self.check(["phone", "webhooks", "--apply", "--json"], logs: "keryx phone webhooks --apply --json", hiding: [])
        Self.check(["phone", "tunnel", "--json"], logs: "keryx phone tunnel --json", hiding: [])
        Self.check(["service", "--json"], logs: "keryx service --json", hiding: [])
        Self.check(["service", "uninstall", "--llm", "--voice", "--json"],
                   logs: "keryx service uninstall --llm --voice --json", hiding: [])
        Self.check(["service", "stop", "--all", "--json"], logs: "keryx service stop --all --json", hiding: [])
        Self.check(["service", "start", "--all", "--json"], logs: "keryx service start --all --json", hiding: [])
        Self.check(["models", "stop", "--json"], logs: "keryx models stop --json", hiding: [])
        Self.check(["voice", "download", "--json"], logs: "keryx voice download --json", hiding: [])
        Self.check(["integrations", "--json"], logs: "keryx integrations --json", hiding: [])
        Self.check(["tools", "--json"], logs: "keryx tools --json", hiding: [])
    }

    @Test func aFlagsValueIsNeverLoggedInEitherSpelling() {
        Self.check(["models", "start", "--runtime=ollama", "--json"],
                   logs: "keryx models start --runtime=… --json", hiding: ["ollama"])
        Self.check(["models", "start", "--runtime", "ollama", "--json"],
                   logs: "keryx models start --runtime … --json", hiding: ["ollama"])
        // Only a listed flag is named: an unknown one is a value, `=` or not.
        Self.check(["models", "start", "--token=sk-very-secret"], logs: "keryx models start …",
                   hiding: ["sk-very", "--token"])
    }

    @Test func signInsLogWhichButNeverTheClientFile() {
        Self.check(["auth", "login", "google-workspace", "--client-file=/Users/ada/Downloads/client.json", "--json"],
                   logs: "keryx auth login google-workspace --client-file=… --json",
                   hiding: ["ada", "Downloads", "client.json"])
        Self.check(["auth", "login", "claude", "--json"], logs: "keryx auth login claude --json", hiding: [])
        Self.check(["auth", "logout", "gmail", "--json"], logs: "keryx auth logout gmail --json", hiding: [])
        // A name it does not know is a value.
        Self.check(["auth", "login", "ada@example.com"], logs: "keryx auth login …", hiding: ["ada"])
    }

    @Test func pluginsLogTheirNameButNeverASetting() {
        Self.check(["plugins", "list", "--json"], logs: "keryx plugins list --json", hiding: [])
        Self.check(["plugins", "hosts", "--json"], logs: "keryx plugins hosts --json", hiding: [])
        Self.check(["plugins", "install", "check_billing", "--set=provider=openai", "--set=monthly_budget=40", "--json"],
                   logs: "keryx plugins install check_billing --set=… --set=… --json",
                   hiding: ["provider", "openai", "budget", "40"])
        Self.check(["plugins", "install", "cluster_stats", "--cluster=della=gpu", "--guard=/Users/ada/guard",
                    "--json"],
                   logs: "keryx plugins install cluster_stats --cluster=… --guard=… --json",
                   hiding: ["della", "gpu", "ada"])
        Self.check(["plugins", "set", "check_billing", "--json", "--", "monthly_budget=40"],
                   logs: "keryx plugins set check_billing --json …", hiding: ["budget", "40"])
        Self.check(["plugins", "remove", "my_own_tool", "--json"], logs: "keryx plugins remove … --json",
                   hiding: ["my_own"])
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
