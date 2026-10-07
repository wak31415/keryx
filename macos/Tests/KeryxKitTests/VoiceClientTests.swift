import Foundation
import Testing
@testable import KeryxKit
import KeryxTestSupport
import KeryxPreview

/// `config options`, `voice` and `voice use`, as `CLIClient` runs them.
@Suite struct VoiceClientTests {
    static func ok(_ stdout: Data = Data(), exit: Int32 = 0) -> CommandResult {
        .init(exitCode: exit, stdout: stdout, stderr: Data())
    }

    static func fixture(_ name: String) throws -> Data {
        try ContractTests.data("fixture:" + name)
    }

    static func words(_ request: CommandRequest?) -> [String] {
        Array((request?.arguments ?? []).dropFirst(KeryxLocation.fixture.prefixArguments.count))
    }

    // MARK: - Options

    @Test func optionsAsksForOneKey() async throws {
        let fake = FakeRunner { _ in Self.ok(PreviewData.data("options-realtime.json")) }
        let client = CLIClient(location: .fixture, runner: fake)
        let list = try await client.options(key: "OPENAI_REALTIME_MODEL")
        #expect(Self.words(fake.requests.last) == ["config", "options", "OPENAI_REALTIME_MODEL", "--json"])
        #expect(list.live && list.freeForm && list.source == "openai")
        #expect(list.options.map(\.value).contains(list.current ?? ""))
    }

    @Test func aBuiltInListWithItsErrorIsAnAnswer() async throws {
        let fake = FakeRunner { _ in Self.ok(PreviewData.data("options-subagent.json")) }
        let list = try await CLIClient(location: .fixture, runner: fake).options(key: "SUBAGENT_MODEL")
        #expect(!list.live)
        #expect(list.error != nil)
        #expect(list.options.first?.label == "Claude Opus")
    }

    @Test func anUnknownKeyIsAFailure() async throws {
        let fake = FakeRunner { _ in .init(exitCode: 2, stdout: Data(), stderr: Data("ASSISTANT_NAME has no options".utf8)) }
        await #expect(throws: KeryxError.failed(command: "keryx config options", exitCode: 2,
                                                 message: "ASSISTANT_NAME has no options")) {
            _ = try await CLIClient(location: .fixture, runner: fake).options(key: "ASSISTANT_NAME")
        }
    }

    // MARK: - Voice

    @Test func voiceReadsTheStatus() async throws {
        let fake = FakeRunner { _ in Self.ok(PreviewData.data("voice-here.json")) }
        let voice = try await CLIClient(location: .fixture, runner: fake).voice()
        #expect(Self.words(fake.requests.last) == ["voice", "--json"])
        #expect(voice.kind == .here)
        #expect(voice.here.reachable)
        #expect(!voice.needsLocalModel)
        #expect(PreviewData.voiceNeedsModel.needsLocalModel)
    }

    @Test func useOpenAI() async throws {
        let fake = FakeRunner { _ in Self.ok(try Self.fixture("voice-use-openai.json")) }
        let result = try await CLIClient(location: .fixture, runner: fake).useVoiceOpenAI()
        #expect(Self.words(fake.requests.last) == ["voice", "use", "openai", "--json"])
        #expect(result.restartNeeded)
    }

    @Test func useServerSendsTheKeyOnStdinOnly() async throws {
        let fake = FakeRunner { _ in Self.ok(try Self.fixture("voice-use-server.json")) }
        let client = CLIClient(location: .fixture, runner: fake)
        let result = try await client.useVoiceServer(url: "http://studio.local:8000/v1", key: "vk-very-secret",
                                                     keepAnyway: false)
        let request = try #require(fake.requests.last)
        #expect(Self.words(request) == ["voice", "use", "server", "http://studio.local:8000/v1", "--key-stdin", "--json"])
        #expect(request.stdin == Data("vk-very-secret".utf8))
        #expect(!request.arguments.joined().contains("vk-very-secret"))
        #expect(!request.environment.values.contains { $0.contains("vk-very-secret") })
        #expect(result.saved?["VOICE_API_KEY"] == .string("(set)"))
        #expect(result.warnings.count == 1)

        _ = try await client.useVoiceServer(url: "http://studio.local:8000/v1", key: nil, keepAnyway: true)
        let second = try #require(fake.requests.last)
        #expect(Self.words(second) == ["voice", "use", "server", "http://studio.local:8000/v1", "--keep-anyway", "--json"])
        #expect(second.stdin == nil)
    }

    @Test func anAddressLikeAnOptionComesAfterTheSeparator() async throws {
        let fake = FakeRunner { _ in Self.ok(try Self.fixture("voice-use-server.json")) }
        _ = try await CLIClient(location: .fixture, runner: fake)
            .useVoiceServer(url: "-weird", key: nil, keepAnyway: false)
        #expect(Self.words(fake.requests.last) == ["voice", "use", "server", "--json", "--", "-weird"])
    }

    @Test func aFailedHandshakeIsAnAnswerNotAnError() async throws {
        let fake = FakeRunner { _ in Self.ok(try Self.fixture("voice-use-server-problem.json"), exit: 1) }
        let result = try await CLIClient(location: .fixture, runner: fake)
            .useVoiceServer(url: "http://studio.local:8000/v1", key: nil, keepAnyway: false)
        #expect(result.saved == nil)
        #expect(result.problem?.contains("handshake") == true)
        #expect(!result.restartNeeded)
    }

    @Test func aRefusedServerIsAFailure() async throws {
        let fake = FakeRunner { _ in .init(exitCode: 2, stdout: Data(), stderr: Data("not a URL".utf8)) }
        await #expect(throws: KeryxError.failed(command: "keryx voice use", exitCode: 2, message: "not a URL")) {
            _ = try await CLIClient(location: .fixture, runner: fake)
                .useVoiceServer(url: "nonsense", key: nil, keepAnyway: false)
        }
    }

    // MARK: - Setting up here

    @Test func voiceHereMapsLinesAndPassesTheVoice() async throws {
        let fake = FakeRunner()
        fake.lineResponder = { _ in
            ["Installing…",
             #"{"event": "step", "step": "install", "message": "Downloading", "progress": {"done": 1, "total": 4}}"#,
             #"{"event": "step", "step": "start", "message": "Starting"}"#,
             #"{"event": "done", "base_url": "http://127.0.0.1:8765/v1"}"#]
        }
        let client = CLIClient(location: .fixture, runner: fake)
        var events: [VoiceSetupEvent] = []
        for try await event in client.voiceHere(voice: "bf_emma") { events.append(event) }
        #expect(events == [
            .step(step: "install", message: "Downloading", progress: .init(done: 1, total: 4)),
            .step(step: "start", message: "Starting", progress: nil),
            .done(baseURL: "http://127.0.0.1:8765/v1"),
        ])
        let request = try #require(fake.requests.last)
        #expect(Self.words(request) == ["voice", "use", "here", "--voice", "bf_emma", "--json"])
        #expect(request.timeout == nil)

        for try await _ in client.voiceHere(voice: nil) {}
        #expect(Self.words(fake.requests.last) == ["voice", "use", "here", "--json"])
    }

    @Test func voiceHereEndingWithOnlyStepsThrows() async throws {
        let fake = FakeRunner()
        fake.lineResponder = { _ in [#"{"event": "step", "step": "install", "message": "Installing"}"#] }
        let client = CLIClient(location: .fixture, runner: fake)
        var events: [VoiceSetupEvent] = []
        await #expect(throws: KeryxError.failed(command: "keryx voice use", exitCode: -1,
                                                 message: VoiceSetupEvent.stoppedWithoutSaying)) {
            for try await event in client.voiceHere(voice: nil) { events.append(event) }
        }
        #expect(events.count == 1)
    }

    @Test func voiceHereEndingWithAnErrorFinishesNormally() async throws {
        let fake = FakeRunner()
        fake.lineResponder = { _ in [#"{"event": "error", "message": "no room"}"#] }
        var events: [VoiceSetupEvent] = []
        for try await event in CLIClient(location: .fixture, runner: fake).voiceHere(voice: nil) {
            events.append(event)
        }
        #expect(events == [.error("no room")])
    }

    @Test func aStepWithoutSizesHasNoProgressAndAMalformedOneIsNoEvent() {
        #expect(VoiceSetupEvent.parse(#"{"event": "step", "step": "unit", "message": "x", "progress": {"done": 1}}"#)
            == .step(step: "unit", message: "x", progress: nil))
        #expect(VoiceSetupEvent.parse(#"{"event": "step", "message": "x"}"#) == nil)
        #expect(VoiceSetupEvent.parse(#"{"event": "done"}"#) == nil)
        #expect(VoiceSetupEvent.parse("not json") == nil)
        #expect(VoiceSetupEvent.parse(#"{"event": "something new"}"#) == nil)
    }
}
