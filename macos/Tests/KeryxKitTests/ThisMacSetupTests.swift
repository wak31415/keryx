import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview

/// Set Up… on This Mac as one action: `models start` first when the model server isn't
/// answering, then `voice use here` once it serves — and the steps each run reached, kept
/// once it ends.
@MainActor
@Suite struct ThisMacSetupTests {
    func makeModel(client: StubClient) async -> AppModel {
        let model = AppModel(locator: StubLocator(), makeClient: { _ in client },
                             preferences: Preferences(defaults: freshDefaults()),
                             timing: AppModelTests.noPoll)
        await model.start()
        await model.refresh(.models)
        return model
    }

    /// The models with the model server meant to serve a model and not answering.
    static var serverDown: ModelsReport {
        var report = PreviewData.models
        report.servers.llm.reachable = false
        report.servers.llm.servedModel = nil
        return report
    }

    /// A voice setup that waits for the test.
    final class ManualSetup: @unchecked Sendable {
        private let lock = NSLock()
        private var continuation: AsyncThrowingStream<VoiceSetupEvent, Error>.Continuation?

        func stream() -> VoiceSetupStream {
            VoiceSetupStream(AsyncThrowingStream { continuation in
                lock.withLock { self.continuation = continuation }
            }, stop: { [weak self] in
                guard let self else { return }
                let continuation = lock.withLock { self.continuation }
                continuation?.yield(.cancelled)
                continuation?.finish()
            })
        }

        func send(_ event: VoiceSetupEvent) { lock.withLock { continuation }?.yield(event) }
        func finish() { lock.withLock { continuation }?.finish() }
    }

    @Test func theModelServerDownStartsItFirstThenTheVoice() async {
        let server = ManualRun<ModelServerStarted>()
        let setup = ManualSetup()
        let client = StubClient {
            $0.models = .success(Self.serverDown)
            $0.modelServer = { _ in server.stream() }
            $0.voiceHere = { _ in setup.stream() }
        }
        let model = await makeModel(client: client)
        defer { model.stop() }
        #expect(model.models.value?.modelServerDown == true)

        model.setUpThisMac(voice: "bf_emma")
        #expect(model.thisMacSetup == ThisMacSetup(startsServer: true))
        #expect(client.calls.last == "models start")
        #expect(!client.calls.contains { $0.hasPrefix("voice use here") }, "the voice waits for the server")
        // Nothing else starts while it runs.
        model.setUpThisMac()
        #expect(client.calls.filter { $0 == "models start" }.count == 1)

        server.send(.step(step: "runtime", message: "Installing llama.cpp with Homebrew"))
        server.send(.done(ModelServerStarted(url: "http://127.0.0.1:8090/v1", servedModel: "qwen3-coder-30b-a3b")))
        server.finish()
        #expect(await eventually { model.voiceSetup.isRunning })
        #expect(client.calls.contains("voice use here --voice bf_emma"))

        setup.send(.done(baseURL: "http://127.0.0.1:8765/v1"))
        setup.finish()
        await model.voiceSetup.settled()
        #expect(model.voiceSetup.phase == .finished(baseURL: "http://127.0.0.1:8765/v1"))
        #expect(model.configNeedsRestart)
    }

    @Test func aServerThatAnswersGoesStraightToTheVoice() async {
        let client = StubClient()
        let model = await makeModel(client: client)
        defer { model.stop() }
        #expect(model.models.value?.modelServerDown == false)
        model.setUpThisMac()
        #expect(model.thisMacSetup == ThisMacSetup(startsServer: false))
        #expect(client.calls.last == "voice use here")
        #expect(!client.calls.contains("models start"))
    }

    @Test func startServerSaysSoWhateverTheModelsSaid() async {
        let server = ManualRun<ModelServerStarted>()
        let client = StubClient { $0.modelServer = { _ in server.stream() } }
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.setUpThisMac(startServer: true)
        #expect(client.calls.last == "models start")
        server.finish()
        await model.modelServerRun.settled()
    }

    @Test func aServerThatFailsNeverStartsTheVoice() async {
        let server = ManualRun<ModelServerStarted>()
        let client = StubClient {
            $0.models = .success(Self.serverDown)
            $0.modelServer = { _ in server.stream() }
        }
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.setUpThisMac()
        server.send(.step(step: "unit", message: "Installing the model server's service"))
        server.send(.error("the model server did not come up"))
        server.finish()
        await model.modelServerRun.settled()
        #expect(model.modelServerRun.phase == .failed("the model server did not come up"))
        #expect(model.modelServerRun.lastStep == "unit", "the step it stopped on is kept")
        #expect(!client.calls.contains { $0.hasPrefix("voice use here") })

        // Dismissing the setup forgets both of its runs.
        model.dismissThisMacSetup()
        #expect(model.modelServerRun.phase == nil && model.voiceSetup.phase == nil)
    }

    @Test func aCancelledServerNeverStartsTheVoice() async {
        let server = ManualRun<ModelServerStarted>()
        let client = StubClient {
            $0.models = .success(Self.serverDown)
            $0.modelServer = { _ in server.stream() }
        }
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.setUpThisMac()
        model.modelServerRun.cancel()
        server.send(.cancelled)
        server.finish()
        await model.modelServerRun.settled()
        #expect(model.modelServerRun.phase == nil)
        #expect(!client.calls.contains { $0.hasPrefix("voice use here") })
    }

    @Test func aServerStartedOnItsOwnIsNoPartOfASetup() async {
        let server = ManualRun<ModelServerStarted>()
        let client = StubClient {
            $0.models = .success(Self.serverDown)
            $0.modelServer = { _ in server.stream() }
        }
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.setUpThisMac()
        server.finish()
        await model.modelServerRun.settled()
        model.voiceSetup.dismiss()

        model.runModelServer()
        #expect(model.thisMacSetup == nil)
        server.send(.done(ModelServerStarted(url: "u", servedModel: "m")))
        server.finish()
        await model.modelServerRun.settled()
        #expect(!client.calls.contains { $0.hasPrefix("voice use here") }, "Local Models' start is only the server")
    }

    @Test func theVoiceSetupKeepsTheStepItStoppedOn() async {
        let setup = ManualSetup()
        let client = StubClient { $0.voiceHere = { _ in setup.stream() } }
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.setUpThisMac()
        setup.send(.step(step: "install", message: "Installing the voice server", progress: nil))
        setup.send(.step(step: "download", message: "Downloading the speech models", progress: nil))
        setup.send(.progress(step: "download", done: 5, total: nil))
        #expect(await eventually { model.voiceSetup.lastStep == "download" })
        setup.send(.error("the connection was lost"))
        setup.finish()
        await model.voiceSetup.settled()
        #expect(model.voiceSetup.phase == .failed("the connection was lost"))
        #expect(model.voiceSetup.lastStep == "download")

        // The next setup starts from nothing.
        model.setUpThisMac()
        #expect(model.voiceSetup.lastStep == "")
    }
}
