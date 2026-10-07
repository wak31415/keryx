import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview

/// `StreamRun`: one `StreamEvent` command at a time, as `VoiceSetup` is one voice setup.
@MainActor
@Suite struct StreamRunTests {
    final class Activities {
        var begun: [String] = []
        var ended = 0
    }

    let activities = Activities()

    func makeRun<Done: StreamDone>(_ done: Done.Type = SignedIn.self) -> StreamRun<Done> {
        let activities = activities
        return StreamRun(beginActivity: { reason in
            activities.begun.append(reason)
            return { activities.ended += 1 }
        })
    }

    typealias Activity = StreamRun<ModelServerStarted>.Activity
    static let started = ModelServerStarted(url: "http://127.0.0.1:8090/v1", servedModel: "qwen3-coder-30b-a3b")

    @Test func stepsAndProgressAreShownAsTheyComeAndDoneFinishes() async {
        let run = makeRun(ModelServerStarted.self)
        let manual = ManualRun<ModelServerStarted>()
        var finished: [ModelServerStarted] = []
        #expect(run.start(reason: "Starting the model server", { manual.stream() }) { finished.append($0) })
        #expect(run.phase == .running(Activity()))
        #expect(run.activity?.message == StreamRun<ModelServerStarted>.starting)
        #expect(activities.begun == ["Starting the model server"])

        manual.send(.step(step: "runtime", message: "Installing llama.cpp"))
        #expect(await eventually { run.activity == Activity(step: "runtime", message: "Installing llama.cpp") })
        manual.send(.progress(step: "runtime", done: 5, total: nil))
        #expect(await eventually { run.activity?.progress == .init(done: 5, total: nil) })
        #expect(run.activity?.progress?.fraction == nil)
        #expect(run.activity?.message == "Installing llama.cpp", "a progress line keeps its step's words")
        manual.send(.step(step: "unit", message: "Installing its service"))
        #expect(await eventually { run.activity?.step == "unit" })
        #expect(run.activity?.progress == nil, "a new step starts with no sizes")

        manual.send(.done(Self.started))
        // Said, but the CLI has not exited: still running.
        try? await Task.sleep(for: .milliseconds(30))
        #expect(run.isRunning && finished.isEmpty)
        manual.finish()
        await run.settled()
        #expect(run.phase == .finished(Self.started))
        #expect(run.result == Self.started)
        #expect(finished == [Self.started])
        #expect(activities.ended == 1)
        run.dismiss()
        #expect(run.phase == nil)
    }

    @Test func aSignInOpensAPageAsksForACodeAndTakesOneLine() async {
        let run = makeRun()
        let manual = ManualRun<SignedIn>()
        run.start(reason: "Signing in", takes: .oneLine, { manual.stream() })
        #expect(manual.inputOpen, "a run that reads a line keeps its stdin open until then")
        manual.send(.open(url: "https://claude.com/cai/oauth/authorize", code: nil))
        #expect(await eventually { run.activity?.link == .init(url: "https://claude.com/cai/oauth/authorize") })
        manual.send(.code)
        #expect(await eventually { run.activity?.wantsCode == true })
        #expect(!run.send("two\nlines"), "one line only")
        #expect(run.activity?.wantsCode == true)
        #expect(run.send("c#s"))
        #expect(run.activity?.wantsCode == false)
        #expect(manual.sent == ["c#s"])
        #expect(!manual.inputOpen, "one line read, its stdin closed")
        #expect(!run.send("again"), "a second line is refused, not left in a pipe")
        manual.send(.done(SignedIn(name: "claude", message: "Signed in.", stored: "CLAUDE_CODE_OAUTH_TOKEN")))
        manual.finish()
        await run.settled()
        #expect(run.result?.stored == "CLAUDE_CODE_OAUTH_TOKEN")
        #expect(!run.send("after"), "nothing runs")
    }

    @Test func codexsDeviceCodeComesWithItsPage() async {
        let run = makeRun()
        let manual = ManualRun<SignedIn>()
        run.start(reason: "Signing in", { manual.stream() })
        manual.send(.open(url: "https://auth.openai.com/codex/device", code: "K7QF-2MXRA"))
        #expect(await eventually { run.activity?.link?.code == "K7QF-2MXRA" })
        #expect(run.activity?.wantsCode == false)
        manual.finish()
        await run.settled()
    }

    @Test func aRunThatReadsNothingHasItsInputClosedAtOnce() async {
        let run = makeRun()
        let manual = ManualRun<SignedIn>()
        run.start(reason: "Signing in", { manual.stream() })
        #expect(!manual.inputOpen)
        #expect(!run.send("x"))
        manual.finish()
        await run.settled()
    }

    @Test func aClosedInputTakesNothing() async {
        let run = makeRun()
        let manual = ManualRun<SignedIn>()
        run.start(reason: "Signing in", { manual.stream() })
        manual.closeInput()
        #expect(!run.send("x"))
        manual.finish()
        await run.settled()
    }

    @Test func cancellingReadsCancellingUntilTheStreamEndsThenGoes() async {
        let run = makeRun(VoiceModelsDownloaded.self)
        let manual = ManualRun<VoiceModelsDownloaded>()
        run.start(reason: "Downloading", { manual.stream() })
        manual.send(.progress(step: "download", done: 1, total: 4))
        #expect(await eventually { run.activity?.progress?.fraction == 0.25 })
        run.cancel()
        #expect(run.phase == .cancelling)
        #expect(manual.stopRequests == 1)
        #expect(run.isBusy && !run.isRunning)
        // Nothing else may start while it stops.
        #expect(!run.start(reason: "Again", { ManualRun<VoiceModelsDownloaded>().stream() }))
        // Late progress does not bring it back.
        manual.send(.progress(step: "download", done: 2, total: 4))
        try? await Task.sleep(for: .milliseconds(20))
        #expect(run.phase == .cancelling)
        manual.send(.cancelled)
        manual.finish()
        await run.settled()
        #expect(run.phase == nil)
        #expect(!run.isBusy)
    }

    @Test func anErrorWhileStoppingIsNoFailureToShow() async {
        let run = makeRun()
        let manual = ManualRun<SignedIn>()
        run.start(reason: "Signing in", { manual.stream() })
        run.cancel()
        manual.send(.error("interrupted"))
        manual.finish()
        await run.settled()
        #expect(run.phase == nil)
    }

    @Test func anErrorFailsAndStaysUntilDismissedOrRestarted() async {
        let run = makeRun()
        let manual = ManualRun<SignedIn>()
        run.start(reason: "Signing in", { manual.stream() })
        manual.send(.error("No address came back, so the sign-in was stopped; start it again."))
        manual.finish()
        await run.settled()
        #expect(run.failure == "No address came back, so the sign-in was stopped; start it again.")
        #expect(!run.isBusy)
        let again = ManualRun<SignedIn>()
        #expect(run.start(reason: "Signing in", { again.stream() }))
        #expect(run.phase == .running(.init()))
        again.finish()
        await run.settled()
    }

    @Test func aStreamThatEndsWithoutAnEndingSaysSo() async {
        let run = makeRun(ModelServerStarted.self)
        let manual = ManualRun<ModelServerStarted>()
        run.start(reason: "Starting", { manual.stream() })
        manual.send(.step(step: "runtime", message: "x"))
        manual.finish()
        await run.settled()
        #expect(run.failure?.message == ModelServerStarted.stoppedWithoutSaying)
    }

    @Test func aGroupThatOutlivedSigkillIsSaidNeverTakenForStopped() async {
        let run = makeRun()
        let manual = ManualRun<SignedIn>()
        run.start(reason: "Signing in", { manual.stream() })
        run.cancel()
        manual.finish(throwing: RunnerError.stillRunning)
        await run.settled()
        #expect(run.failure?.message == RunnerError.stillRunning.localizedDescription)
    }

    @Test func aCLIFailureIsItsMessage() async {
        let run = makeRun()
        let manual = ManualRun<SignedIn>()
        run.start(reason: "Signing in", { manual.stream() })
        manual.finish(throwing: KeryxError.launch("no keryx"))
        await run.settled()
        #expect(run.failure?.message == KeryxError.launch("no keryx").message)
    }

    @Test func thePreviewReplaysEachFixtureToItsEnd() async {
        let client = PreviewClient()
        let start = makeRun(ModelServerStarted.self)
        start.start(reason: "x", { client.startModelServer(runtime: nil) })
        await start.settled()
        #expect(start.result == Self.started)

        let download = makeRun(VoiceModelsDownloaded.self)
        download.start(reason: "x", { client.downloadVoiceModels() })
        await download.settled()
        #expect(download.result == VoiceModelsDownloaded(stt: "parakeet-tdt", tts: "kokoro"))

        for provider in SignInProvider.allCases {
            let signIn = makeRun()
            signIn.start(reason: "x", { client.signIn(provider, clientFile: nil) })
            await signIn.settled()
            #expect(signIn.result?.name == provider.rawValue)
        }
    }

    @Test func aScriptedSignInAnswersTheLineItWasSent() async {
        let client = PreviewClient(signIn: .answering([.open(url: "https://x.example", code: nil), .code],
                                                      then: [.done(SignedIn(name: "claude", message: "In."))]))
        let run = makeRun()
        run.start(reason: "x", takes: .oneLine, { client.signIn(.claude, clientFile: nil) })
        #expect(await eventually { run.activity?.wantsCode == true })
        #expect(run.send("c#s"))
        await run.settled()
        #expect(run.result?.message == "In.")
    }
}
