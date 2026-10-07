import Foundation
import Testing
@testable import KeryxKit
import KeryxTestSupport
import KeryxPreview

/// Setup, the PIN, verification, the phone, the service, sign-ins and plugins, as
/// `CLIClient` runs them: the command line, what goes on stdin (and never on argv), and
/// which exits are answers.
@Suite struct SetupClientTests {
    static func ok(_ stdout: Data = Data(), exit: Int32 = 0) -> CommandResult {
        .init(exitCode: exit, stdout: stdout, stderr: Data())
    }

    static func refused(_ message: String, exit: Int32) -> CommandResult {
        .init(exitCode: exit, stdout: Data(), stderr: Data((message + "\n").utf8))
    }

    static func data(_ name: String) throws -> Data { try ContractTests.data(name) }

    static func words(_ request: CommandRequest?) -> [String] {
        Array((request?.arguments ?? []).dropFirst(KeryxLocation.fixture.prefixArguments.count))
    }

    /// A client whose every command answers `stdout` (exit `exit`), and the runner behind it.
    static func client(_ stdout: Data, exit: Int32 = 0) -> (CLIClient, FakeRunner) {
        let fake = FakeRunner { _ in ok(stdout, exit: exit) }
        return (CLIClient(location: .fixture, runner: fake), fake)
    }

    static func secretFree(_ request: CommandRequest, _ secret: String) -> Bool {
        !request.arguments.joined(separator: " ").contains(secret)
            && !request.environment.values.contains { $0.contains(secret) }
    }

    // MARK: - Setup and the PIN

    @Test func setupPlanAndMark() async throws {
        let (client, fake) = Self.client(PreviewData.data("setup-plan-partway.json"))
        let plan = try await client.setupPlan()
        #expect(Self.words(fake.requests.last) == ["setup", "plan", "--json"])
        #expect(fake.requests.last?.timeout == .seconds(60))
        #expect(plan.step("pin")?.kind == .done)
        #expect(plan.remaining.map(\.id) == ["voice", "agents", "phone", "service"])

        let (marker, marks) = Self.client(try Self.data("fixture:setup-mark.json"))
        let marked = try await marker.markSetupStep("google")
        #expect(Self.words(marks.last) == ["setup", "mark", "google", "--json"])
        #expect(marked == SetupMarkResult(id: "google", walked: true))
    }

    @Test func aRefusedMarkIsTheCLIsSentence() async throws {
        let fake = FakeRunner { _ in Self.refused("Voice is required; finish it in the app", exit: 2) }
        await #expect(throws: KeryxError.failed(command: "keryx setup mark", exitCode: 2,
                                                 message: "Voice is required; finish it in the app")) {
            _ = try await CLIClient(location: .fixture, runner: fake).markSetupStep("voice")
        }
    }

    @Test func pinStatusSaysWhereNeverWhat() async throws {
        let (client, fake) = Self.client(PreviewData.data("pin-locked.json"))
        let pin = try await client.pin()
        #expect(Self.words(fake.requests.last) == ["pin", "--json"])
        #expect(pin.set && pin.locked && pin.place == .file)
        #expect(pin.lockedUntil == ISO8601DateFormatter().date(from: "2026-10-07T18:30:00Z"))
        #expect(PreviewData.pinEnvironment.place == .environment)
        #expect(PreviewData.pinNone.enrolmentOpen && !PreviewData.pinNone.set)
    }

    @Test func thePinGoesOnStdinTwiceNeverArgv() async throws {
        let (client, fake) = Self.client(try Self.data("fixture:pin-set.json"))
        let result = try await client.setPin("482915", replace: false)
        let request = try #require(fake.requests.last)
        #expect(Self.words(request) == ["pin", "set", "--stdin", "--json"])
        #expect(request.stdin == Data("482915\n482915\n".utf8))
        #expect(Self.secretFree(request, "482915"))
        #expect(result == PinSetResult(set: true, replaced: false, restartNeeded: true))

        _ = try await client.setPin("482915", replace: true)
        #expect(Self.words(fake.requests.last) == ["pin", "set", "--stdin", "--replace", "--json"])
    }

    @Test func aRefusedPinIsAFailureNotAnAnswer() async throws {
        let fake = FakeRunner { _ in Self.refused("a PIN is already set; --replace replaces it", exit: 1) }
        await #expect(throws: KeryxError.failed(command: "keryx pin set", exitCode: 1,
                                                 message: "a PIN is already set; --replace replaces it")) {
            _ = try await CLIClient(location: .fixture, runner: fake).setPin("482915", replace: false)
        }
    }

    // MARK: - Verifying a key

    @Test func aTypedKeyIsVerifiedFromStdinOneLine() async throws {
        let (client, fake) = Self.client(try Self.data("fixture:verify-problem.json"))
        let result = try await client.verify(key: "OPENAI_API_KEY", value: "  sk-very-secret\n")
        let request = try #require(fake.requests.last)
        #expect(Self.words(request) == ["config", "verify", "OPENAI_API_KEY", "--stdin", "--json"])
        #expect(request.stdin == Data("sk-very-secret\n".utf8))
        #expect(Self.secretFree(request, "sk-very-secret"))
        #expect(!result.ok && result.problem != nil)
    }

    @Test func aTwilioPairIsTwoLinesOnStdin() async throws {
        let (client, fake) = Self.client(try Self.data("fixture:verify-twilio.json"))
        let sid = "AC" + String(repeating: "0", count: 32)
        let result = try await client.verifyTwilio(accountSID: sid, authToken: "tok-very-secret")
        let request = try #require(fake.requests.last)
        #expect(Self.words(request) == ["config", "verify", "TWILIO_AUTH_TOKEN", "--stdin", "--pair", "--json"])
        #expect(request.stdin == Data("\(sid)\ntok-very-secret\n".utf8))
        #expect(Self.secretFree(request, "tok-very-secret") && Self.secretFree(request, sid))
        #expect(result.ok && result.detail == "Ada's account")
    }

    @Test func aKeyThatCannotBeCheckedIsAFailure() async throws {
        let fake = FakeRunner { _ in Self.refused("ASSISTANT_NAME can't be checked", exit: 2) }
        await #expect(throws: KeryxError.self) {
            _ = try await CLIClient(location: .fixture, runner: fake).verify(key: "ASSISTANT_NAME", value: "Vega")
        }
    }

    @Test func oneLineIsTrimmedAndTwoAreRefusedNeverCut() throws {
        #expect(try CLIClient.oneLine("  a b \n", "The key") == "a b")
        #expect(try CLIClient.oneLine("\n\n", "The key") == "")
        #expect(throws: KeryxError.refused("The key has a line break in it; paste it again as one line.")) {
            try CLIClient.oneLine("first\nsecond", "The key")
        }
    }

    @Test func aKeyOrPairWithALineBreakRunsNothing() async throws {
        let (client, fake) = Self.client(try Self.data("fixture:verify-problem.json"))
        await #expect(throws: KeryxError.refused(
            "OPENAI_API_KEY has a line break in it; paste it again as one line.")) {
            _ = try await client.verify(key: "OPENAI_API_KEY", value: "sk-one\nsk-two")
        }
        await #expect(throws: KeryxError.refused(
            "The auth token has a line break in it; paste it again as one line.")) {
            _ = try await client.verifyTwilio(accountSID: "AC1", authToken: "a\r\nb")
        }
        #expect(fake.requests.isEmpty)
    }

    @Test func aPinWithALineBreakRunsNothing() async throws {
        let (client, fake) = Self.client(try Self.data("fixture:pin-set.json"))
        await #expect(throws: KeryxError.refused(CLIClient.pinOnOneLine)) {
            _ = try await client.setPin("4829\n15", replace: false)
        }
        #expect(fake.requests.isEmpty)
    }

    // MARK: - The phone

    @Test func thePhoneReads() async throws {
        let (numbers, numbersRunner) = Self.client(PreviewData.data("phone-numbers.json"))
        let list = try await numbers.phoneNumbers()
        #expect(Self.words(numbersRunner.last) == ["phone", "numbers", "--json"])
        #expect(list.numbers.map(\.pointsHere) == [false, true])

        let (hooks, hooksRunner) = Self.client(PreviewData.data("phone-webhooks.json"))
        let webhooks = try await hooks.phoneWebhooks()
        #expect(Self.words(hooksRunner.last) == ["phone", "webhooks", "--json"])
        #expect(!webhooks.matches)
        #expect(webhooks.expected.voiceUrl == "https://keryx.example.com/twilio/voice")
        #expect(webhooks.current?.statusUrl == "https://old.example.com/status")
        #expect(PreviewData.phoneWebhooksUnset.current == nil && PreviewData.phoneWebhooksUnset.error != nil)

        let (tunnel, tunnelRunner) = Self.client(PreviewData.data("phone-tunnel-unset.json"))
        let report = try await tunnel.phoneTunnel()
        #expect(Self.words(tunnelRunner.last) == ["phone", "tunnel", "--json"])
        #expect(report.reachable == nil && report.publicHost == nil)
    }

    @Test func applyingTheWebhooksIsItsOwnCommand() async throws {
        let (client, fake) = Self.client(try Self.data("fixture:webhooks-apply.json"))
        let applied = try await client.applyPhoneWebhooks()
        #expect(Self.words(fake.requests.last) == ["phone", "webhooks", "--apply", "--json"])
        #expect(applied == WebhooksApplied(applied: true, number: "+15550001111"))

        let refusing = FakeRunner { _ in Self.refused("There's no TWILIO_NUMBER saved.", exit: 2) }
        await #expect(throws: KeryxError.failed(command: "keryx phone webhooks", exitCode: 2,
                                                 message: "There's no TWILIO_NUMBER saved.")) {
            _ = try await CLIClient(location: .fixture, runner: refusing).applyPhoneWebhooks()
        }
    }

    // MARK: - The service

    @Test func theServiceAndItsInstallers() async throws {
        let (client, fake) = Self.client(PreviewData.data("service.json"))
        let service = try await client.service()
        #expect(Self.words(fake.requests.last) == ["service", "--json"])
        #expect(service.units.agent.active && !service.units.voice.installed)

        let (installer, installs) = Self.client(try Self.data("fixture:service-install.json"))
        let installed = try await installer.installService()
        #expect(Self.words(installs.last) == ["service", "install", "--json"])
        #expect(installs.last?.timeout == CLIClient.installerTimeout)
        #expect(installed.ok)

        _ = try await installer.uninstallService()
        #expect(Self.words(installs.last) == ["service", "uninstall", "--json"])
        _ = try await installer.stopModelServer()
        #expect(Self.words(installs.last) == ["models", "stop", "--json"])
    }

    @Test func anInstallerThatFailedIsItsStderr() async throws {
        let fake = FakeRunner { _ in Self.refused("install-launchd.sh exited 1", exit: 1) }
        await #expect(throws: KeryxError.failed(command: "keryx service install", exitCode: 1,
                                                 message: "install-launchd.sh exited 1")) {
            _ = try await CLIClient(location: .fixture, runner: fake).installService()
        }
    }

    // MARK: - Streams

    @Test func startingTheModelServerStreamsItsSteps() async throws {
        let fake = FakeRunner()
        fake.lineResponder = { _ in PreviewData.modelsStartLines }
        let client = CLIClient(location: .fixture, runner: fake)
        var events: [ModelServerEvent] = []
        for try await event in client.startModelServer(runtime: nil) { events.append(event) }
        #expect(Self.words(fake.requests.last) == ["models", "start", "--json"])
        #expect(fake.requests.last?.timeout == nil)
        #expect(events.first == .step(step: "runtime", message: "Installing llama.cpp with Homebrew"))
        #expect(events.contains(.progress(step: "runtime", done: 9_437_184, total: nil)))
        #expect(events.last == .done(ModelServerStarted(url: "http://127.0.0.1:8090/v1",
                                                         servedModel: "qwen3-coder-30b-a3b")))

        for try await _ in client.startModelServer(runtime: "ollama") {}
        #expect(Self.words(fake.requests.last) == ["models", "start", "--runtime=ollama", "--json"])
    }

    @Test func aStreamEndingWithoutAnEndingThrowsItsWords() async throws {
        let fake = FakeRunner()
        fake.lineResponder = { _ in [#"{"event": "step", "step": "download", "message": "Downloading"}"#] }
        let client = CLIClient(location: .fixture, runner: fake)
        await #expect(throws: KeryxError.failed(command: "keryx voice download", exitCode: -1,
                                                 message: VoiceModelsDownloaded.stoppedWithoutSaying)) {
            for try await _ in client.downloadVoiceModels() {}
        }
        #expect(Self.words(fake.requests.last) == ["voice", "download", "--json"])
    }

    @Test func aSignInTakesTheCodeOnItsStdin() async throws {
        let fake = FakeRunner()
        let session = FakeSession(lines: PreviewData.signInLines(.claude).prefix(2).map { $0 })
        session.onSend = { _, session in
            session.yield(PreviewData.signInLines(.claude)[2])
            session.finish()
        }
        fake.sessionResponder = { _ in session }
        let stream = CLIClient(location: .fixture, runner: fake).signIn(.claude, clientFile: nil)
        #expect(stream.acceptsInput)
        var events: [SignInEvent] = []
        for try await event in stream {
            events.append(event)
            if event == .code { #expect(stream.send("c#s-very-secret")) }
        }
        let request = try #require(fake.requests.last)
        #expect(Self.words(request) == ["auth", "login", "claude", "--json"])
        #expect(request.stdin == nil && Self.secretFree(request, "c#s-very-secret"))
        #expect(session.sent == ["c#s-very-secret"])
        #expect(events.map(\.name) == ["open", "code", "done"])
        #expect(events.last == .done(SignedIn(name: "claude", message: "Signed in to Claude with your subscription.",
                                              stored: "CLAUDE_CODE_OAUTH_TOKEN")))
    }

    @Test func googlesClientFileIsOneWordSoAPathLikeAnOptionIsStillAPath() async throws {
        let fake = FakeRunner()
        fake.lineResponder = { _ in PreviewData.signInLines(.googleWorkspace) }
        let client = CLIClient(location: .fixture, runner: fake)
        for try await _ in client.signIn(.googleWorkspace, clientFile: "-/Downloads/client.json") {}
        #expect(Self.words(fake.requests.last) == ["auth", "login", "google-workspace",
                                                   "--client-file=-/Downloads/client.json", "--json"])
        for try await _ in client.signIn(.gmail, clientFile: nil) {}
        #expect(Self.words(fake.requests.last) == ["auth", "login", "gmail", "--json"])
    }

    @Test func stoppingASignInStopsItsChild() async throws {
        let fake = FakeRunner()
        let session = FakeSession(lines: [PreviewData.signInLines(.codex)[0]])
        fake.sessionResponder = { _ in session }
        let stream = CLIClient(location: .fixture, runner: fake).signIn(.codex, clientFile: nil)
        stream.stop()
        #expect(session.stopRequests == 1)
        #expect(!stream.send("x"), "stopped, it takes nothing")
        session.finish()
    }

    // MARK: - Accounts

    @Test func accountsAndSigningOut() async throws {
        let (client, fake) = Self.client(PreviewData.data("integrations.json"))
        let report = try await client.integrations()
        #expect(Self.words(fake.requests.last) == ["integrations", "--json"])
        #expect(report.integration("slack")?.connected == true)
        #expect(report.integration("gmail")?.signInProvider == .gmail)
        #expect(report.integration("github")?.signInProvider == nil, "gh signs itself in")

        let (signer, signs) = Self.client(try Self.data("fixture:auth-logout-gmail.json"))
        let out = try await signer.signOut(.gmail)
        #expect(Self.words(signs.last) == ["auth", "logout", "gmail", "--json"])
        #expect(!out.signedIn)
    }

    // MARK: - Plugins

    @Test func pluginsToolsAndHostsRead() async throws {
        let (client, fake) = Self.client(PreviewData.data("plugins.json"))
        let plugins = try await client.plugins()
        #expect(Self.words(fake.requests.last) == ["plugins", "list", "--json"])
        #expect(plugins.plugin("check_email")?.refused != nil)

        let (hosts, hostRunner) = Self.client(PreviewData.data("ssh-hosts.json"))
        let found = try await hosts.sshHosts()
        #expect(Self.words(hostRunner.last) == ["plugins", "hosts", "--json"])
        #expect(found.hosts.filter(\.controlMaster).map(\.alias) == ["cluster"])
    }

    @Test func toolsExitingOneIsStillTheReport() async throws {
        let (client, fake) = Self.client(PreviewData.data("tools.json"), exit: 1)
        let tools = try await client.tools()
        #expect(Self.words(fake.requests.last) == ["tools", "--json"])
        #expect(tools.refused.map(\.file) == ["broken.py", "check_email.py"])
    }

    @Test func installingAPluginNamesEachValueInOneWordInKeyOrder() async throws {
        let (client, fake) = Self.client(try Self.data("fixture:plugins-install.json"))
        let result = try await client.installPlugin("cluster_stats", values: PluginValues(
            settings: ["timeout_s": "20", "guard": "-x"], clusters: ["della": "gpu", "adroit": "all"],
            guardScript: "/Users/you/bin/guard"))
        #expect(Self.words(fake.requests.last) == [
            "plugins", "install", "cluster_stats", "--set=guard=-x", "--set=timeout_s=20",
            "--cluster=adroit=all", "--cluster=della=gpu", "--guard=/Users/you/bin/guard", "--json",
        ])
        #expect(result.on)

        _ = try await client.installPlugin("check_billing", values: PluginValues())
        #expect(Self.words(fake.requests.last) == ["plugins", "install", "check_billing", "--json"])
        _ = try await client.removePlugin("check_billing")
        #expect(Self.words(fake.requests.last) == ["plugins", "remove", "check_billing", "--json"])
    }

    @Test func settingAPluginPutsItsValuesAfterTheSeparator() async throws {
        let (client, fake) = Self.client(try Self.data("fixture:plugins-set.json"))
        _ = try await client.setPlugin("check_billing", values: PluginValues(settings: ["monthly_budget": "40",
                                                                                       "provider": "openai"]))
        #expect(Self.words(fake.requests.last) == [
            "plugins", "set", "check_billing", "--json", "--", "monthly_budget=40", "provider=openai",
        ])
        _ = try await client.setPlugin("cluster_stats", values: PluginValues(clusters: ["della": "gpu"]))
        #expect(Self.words(fake.requests.last) == ["plugins", "set", "cluster_stats", "--cluster=della=gpu", "--json"])
    }

    @Test func aPluginStillRefusedIsTheCLIsSentence() async throws {
        let fake = FakeRunner { _ in
            Self.refused("check_email is installed but refused: not signed in to Gmail", exit: 1)
        }
        await #expect(throws: KeryxError.failed(command: "keryx plugins install", exitCode: 1,
                                                 message: "check_email is installed but refused: not signed in to Gmail")) {
            _ = try await CLIClient(location: .fixture, runner: fake).installPlugin("check_email", values: .init())
        }
    }
}

private extension FakeRunner {
    var last: CommandRequest? { requests.last }
}
