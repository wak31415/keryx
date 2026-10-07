import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview

@MainActor
@Suite struct VoiceSetupTests {
    final class Activities {
        var begun = 0
        var ended = 0
    }

    let activities = Activities()

    func makeSetup() -> VoiceSetup {
        let activities = activities
        return VoiceSetup(beginActivity: { _ in
            activities.begun += 1
            return { activities.ended += 1 }
        })
    }

    func client(_ setup: ManualSetup) -> StubClient {
        StubClient { $0.voiceHere = { _ in setup.stream() } }
    }

    func client(events: [VoiceSetupEvent], throwing error: Error? = nil) -> StubClient {
        StubClient {
            $0.voiceHere = { _ in
                VoiceSetupStream(AsyncThrowingStream { continuation in
                    for event in events { continuation.yield(event) }
                    continuation.finish(throwing: error)
                })
            }
        }
    }

    static let install = VoiceSetupEvent.step(step: "install", message: "Installing the voice server", progress: nil)

    @Test func theSpeechModelsDownloadShowsItsBytesUnderItsStepsWords() async {
        let setup = makeSetup()
        let manual = ManualSetup()
        setup.start(voice: nil, client: client(manual), onFinish: {})
        manual.send(.step(step: "download", message: "Downloading the voice's speech models", progress: nil))
        manual.send(.progress(step: "download", done: 1, total: 4))
        #expect(await eventually {
            setup.phase == .running(step: "download", message: "Downloading the voice's speech models",
                                    progress: .init(done: 1, total: 4))
        })
        // A size Hugging Face does not say is 0: the bar has no end.
        manual.send(.progress(step: "download", done: 2, total: nil))
        #expect(await eventually {
            setup.phase == .running(step: "download", message: "Downloading the voice's speech models",
                                    progress: .init(done: 2, total: 0))
        })
        manual.send(.step(step: "configure", message: "Saving", progress: nil))
        #expect(await eventually { setup.phase == .running(step: "configure", message: "Saving", progress: nil) })
        manual.finish()
        await setup.settled()
    }

    @Test func stepsAreShownAsTheyComeAndDoneFinishes() async {
        let setup = makeSetup()
        let manual = ManualSetup()
        var finished = 0
        setup.start(voice: nil, client: client(manual), onFinish: { finished += 1 })
        #expect(setup.phase == .running(step: "", message: VoiceSetup.starting, progress: nil))
        manual.send(Self.install)
        #expect(await eventually {
            setup.phase == .running(step: "install", message: "Installing the voice server", progress: nil)
        })
        let sizes = VoiceSetupEvent.Progress(done: 1, total: 4)
        manual.send(.step(step: "install", message: "Downloading", progress: sizes))
        #expect(await eventually { setup.phase == .running(step: "install", message: "Downloading", progress: sizes) })
        manual.send(.done(baseURL: "http://127.0.0.1:8765/v1"))
        // Said, but the CLI has not exited: still running.
        try? await Task.sleep(for: .milliseconds(30))
        #expect(setup.isRunning)
        #expect(finished == 0)
        manual.finish()
        await setup.settled()
        #expect(setup.phase == .finished(baseURL: "http://127.0.0.1:8765/v1"))
        #expect(finished == 1)
        #expect(activities.begun == 1 && activities.ended == 1)
    }

    @Test func cancelStopsAndStaysCancellingUntilTheStreamEnds() async {
        let setup = makeSetup()
        let manual = ManualSetup()
        let stub = client(manual)
        setup.start(voice: "bf_emma", client: stub, onFinish: {})
        #expect(stub.calls == ["voice use here --voice bf_emma"])
        manual.send(Self.install)
        #expect(await eventually { setup.phase?.isStep("install") == true })

        setup.cancel()
        #expect(manual.stopRequests == 1)
        #expect(setup.phase == .cancelling)
        #expect(setup.isBusy && !setup.isRunning)
        // A second press does nothing more; a late step does not bring it back; nothing starts.
        setup.cancel()
        #expect(manual.stopRequests == 1)
        manual.send(.step(step: "start", message: "Starting", progress: nil))
        setup.start(voice: nil, client: stub, onFinish: {})
        #expect(stub.calls.count == 1)
        manual.send(.cancelled)
        try? await Task.sleep(for: .milliseconds(30))
        #expect(setup.phase == .cancelling)

        manual.finish()
        await setup.settled()
        #expect(setup.phase == nil)
        #expect(!setup.isBusy)
        #expect(activities.begun == 1 && activities.ended == 1)
    }

    @Test func aCancelledSetupThatErrsOnTheWayOutIsJustGone() async {
        let setup = makeSetup()
        let manual = ManualSetup()
        setup.start(voice: nil, client: client(manual), onFinish: {})
        setup.cancel()
        manual.finish(throwing: KeryxError.launch("gone"))
        await setup.settled()
        #expect(setup.phase == nil)
    }

    @Test func aGroupThatOutlivedTheStopIsShown() async {
        let setup = makeSetup()
        let manual = ManualSetup()
        setup.start(voice: nil, client: client(manual), onFinish: {})
        setup.cancel()
        manual.send(.cancelled)
        manual.finish(throwing: RunnerError.stillRunning)
        await setup.settled()
        #expect(setup.phase == .failed(RunnerError.stillRunning.localizedDescription))
        #expect(!setup.isBusy)
    }

    @Test func anErrorEventFailsAndCanBeDismissedOrTriedAgain() async {
        let setup = makeSetup()
        var finished = false
        setup.start(voice: nil, client: client(events: [Self.install, .error("No room for the speech models")]),
                    onFinish: { finished = true })
        await setup.settled()
        #expect(setup.phase == .failed("No room for the speech models"))
        #expect(!finished)
        setup.dismiss()
        #expect(setup.phase == nil)

        setup.start(voice: nil, client: client(events: [.done(baseURL: "http://127.0.0.1:8765/v1")]),
                    onFinish: { finished = true })
        await setup.settled()
        #expect(setup.phase == .finished(baseURL: "http://127.0.0.1:8765/v1"))
        #expect(finished)
        #expect(activities.begun == 2 && activities.ended == 2)
    }

    @Test func aStreamThatStopsWithoutATerminalEventFails() async {
        let setup = makeSetup()
        setup.start(voice: nil, client: client(events: [Self.install]), onFinish: {})
        await setup.settled()
        #expect(setup.phase == .failed(VoiceSetupEvent.stoppedWithoutSaying))
    }

    @Test func aCancelledEventEndsWithNoPhase() async {
        let setup = makeSetup()
        setup.start(voice: nil, client: client(events: [Self.install, .cancelled]), onFinish: {})
        await setup.settled()
        #expect(setup.phase == nil)
    }

    @Test func aThrownErrorFailsWithItsSentence() async {
        let setup = makeSetup()
        setup.start(voice: nil, client: client(events: [], throwing: KeryxError.launch("No uv")), onFinish: {})
        await setup.settled()
        #expect(setup.phase == .failed("No uv."))
    }

    @Test func thePreviewClientReplaysTheFixtureAndStopsWhenAsked() async {
        let setup = makeSetup()
        setup.start(voice: nil, client: PreviewClient(), onFinish: {})
        await setup.settled()
        #expect(setup.phase == .finished(baseURL: "http://127.0.0.1:8765/v1"))

        setup.start(voice: nil, client: PreviewClient(setup: .holding([Self.install])), onFinish: {})
        #expect(await eventually { setup.phase?.isStep("install") == true })
        setup.cancel()
        await setup.settled()
        #expect(setup.phase == nil)
    }

    /// End to end, with a shell standing in for `keryx voice use here`: a step, a
    /// grandchild (an installer), and `cancelled` on SIGTERM.
    @Test func cancellingARealSetupWaitsForTheWholeGroupToGo() async throws {
        let file = NSTemporaryDirectory() + "keryx-voice-\(UUID().uuidString)"
        defer { try? FileManager.default.removeItem(atPath: file) }
        let script = """
            trap 'echo "{\\"event\\": \\"cancelled\\"}"; exit 130' TERM
            sleep 30 & echo $! > '\(file)'
            echo '{"event": "step", "step": "install", "message": "Installing"}'
            wait
            """
        var location = KeryxLocation.fixture
        location.executable = "/bin/sh"
        location.prefixArguments = ["-c", script, "keryx"]
        location.checkout = "/tmp"
        location.workingDirectory = "/tmp"
        let client = CLIClient(location: location, runner: ProcessRunner())
        let setup = makeSetup()
        setup.start(voice: nil, client: client, onFinish: {})
        #expect(await eventually { setup.phase?.isStep("install") == true })
        let grandchild = try await ProcessRunnerTests.pid(writtenTo: file)

        setup.cancel()
        #expect(setup.phase == .cancelling)
        await setup.settled()
        #expect(setup.phase == nil)
        #expect(await ProcessRunnerTests.isGone(grandchild))
    }
}

extension VoiceSetup.Phase {
    func isStep(_ name: String) -> Bool {
        if case let .running(step, _, _) = self { step == name } else { false }
    }
}
