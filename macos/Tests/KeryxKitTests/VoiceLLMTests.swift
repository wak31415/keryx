import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview

/// Where a voice on this Mac takes its words from: `voice llm` chosen in `voiceLLMRun`, its
/// own server started and stopped, and This Mac's Set Up… waiting on the agent's model
/// server only while the words are the agent's.
@MainActor
@Suite struct VoiceLLMTests {
    func makeModel(client: StubClient) async -> AppModel {
        let model = AppModel(locator: StubLocator(), makeClient: { _ in client },
                             preferences: Preferences(defaults: freshDefaults()),
                             timing: AppModelTests.noPoll)
        await model.start()
        await model.refresh(.models, .voice)
        return model
    }

    static let chosen = VoiceLLMChosen(source: "here", model: "qwen3.5-4b", url: "http://127.0.0.1:8091/v1",
                                       voiceRestarted: true, voiceRestartNeeded: false)

    @Test func choosingTheVoiceLLMRunsOneAtATimeAndReadsEverythingAgain() async {
        let run = ManualRun<VoiceLLMChosen>()
        let client = StubClient { $0.voiceLLM = { _ in run.stream() } }
        let model = await makeModel(client: client)
        defer { model.stop() }
        #expect(model.canChangeVoiceLLM)
        #expect(model.chooseVoiceLLM(.here(model: "qwen3.5-4b")))
        #expect(client.calls.last == "voice llm here qwen3.5-4b")
        #expect(!model.canChangeVoiceLLM && model.voiceLLMBusy && model.isBusy)
        // Nothing else that reads it starts meanwhile.
        #expect(!model.chooseVoiceLLM(.shared))
        #expect(!model.runVoiceLLMServer())
        model.setUpThisMac()
        #expect(!client.calls.contains("voice use here"))

        let before = client.calls.count
        run.send(.step(step: "download", message: "Downloading Qwen3.5 4B for the voice LLM (2.7 GB)"))
        run.send(.done(Self.chosen))
        run.finish()
        await model.voiceLLMRun.settled()
        #expect(model.voiceLLMRun.result == Self.chosen)
        #expect(model.voiceLLMRun.seenSteps == ["download"])
        #expect(await eventually {
            Set(client.calls.dropFirst(before)).isSuperset(of: ["voice", "config list", "models list", "service"])
        })
        // The voice server here reads it, not Keryx: no restart of Keryx is asked for.
        #expect(!model.configNeedsRestart)
    }

    @Test func aRemoteChoiceIsHandedOnAsItIs() async {
        let client = StubClient()
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.chooseVoiceLLM(.remote(url: "https://api.openai.com/v1", model: "gpt-5-mini", key: .typed("sk-x")))
        await model.voiceLLMRun.settled()
        #expect(client.calls.contains("voice llm remote https://api.openai.com/v1 --model gpt-5-mini --key-stdin"))
    }

    @Test func noChoiceWhileTheVoiceIsBeingSetUp() async {
        let setup = ThisMacSetupTests.ManualSetup()
        let client = StubClient { $0.voiceHere = { _ in setup.stream() } }
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.setUpThisMac()
        #expect(model.voiceSetup.isRunning)
        #expect(!model.canChangeVoiceLLM)
        #expect(!model.chooseVoiceLLM(.shared))
        #expect(!client.calls.contains("voice llm shared"))
        setup.finish()
        await model.voiceSetup.settled()
    }

    @Test func theVoiceLLMsServerStartsAndStops() async {
        let run = ManualRun<ModelServerStarted>()
        let client = StubClient { $0.voiceLLMServer = { run.stream() } }
        let model = await makeModel(client: client)
        defer { model.stop() }
        #expect(model.runVoiceLLMServer())
        #expect(client.calls.last == "models start --voice-llm")
        // Not stopped while it starts.
        #expect(await model.stopVoiceLLMServer() == .failure(.refused(AppModel.voiceLLMServerInUse)))
        run.send(.done(ModelServerStarted(url: "http://127.0.0.1:8091/v1", servedModel: "qwen3.5-4b")))
        run.finish()
        await model.voiceLLMServerRun.settled()
        #expect(await model.stopVoiceLLMServer() == .success(ServiceResult(ok: true, message: "done")))
        #expect(client.calls.contains("service uninstall --voice-llm"))
        #expect(!model.stoppingVoiceLLMServer)
    }

    @Test func cancellingTheRunsStopsTheVoiceLLMToo() async {
        let run = ManualRun<VoiceLLMChosen>()
        let client = StubClient { $0.voiceLLM = { _ in run.stream() } }
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.chooseVoiceLLM(.shared)
        model.cancelRuns()
        #expect(model.voiceLLMRun.phase == .cancelling)
        #expect(run.stopRequests == 1)
        run.send(.cancelled)
        run.finish()
        await model.voiceLLMRun.settled()
        #expect(model.voiceLLMRun.phase == nil)
    }

    @Test func aVoiceWithWordsOfItsOwnIsSetUpWithoutTheAgentsServer() async {
        let client = StubClient {
            $0.models = .success(ThisMacSetupTests.serverDown)
            $0.voice = .success(PreviewData.voiceOwnLLM)
        }
        let model = await makeModel(client: client)
        defer { model.stop() }
        #expect(model.models.value?.modelServerDown == true)
        #expect(!model.voiceWordsAreTheAgents)
        model.setUpThisMac()
        #expect(model.thisMacSetup == ThisMacSetup(startsServer: false))
        #expect(client.calls.last == "voice use here")
        #expect(!client.calls.contains("models start"))
        await model.voiceSetup.settled()
    }

    @Test func aVoiceSetupAnnouncesItsFirstSetUpsVoiceLLM() async {
        let client = StubClient {
            $0.voiceHere = { _ in
                VoiceSetupStream(AsyncThrowingStream {
                    $0.yield(.step(step: "voice_llm", message: "Downloading Qwen3.5 9B for the voice LLM",
                                   progress: nil))
                    $0.yield(.step(step: "install", message: "Installing the voice server", progress: nil))
                    $0.yield(.done(baseURL: "http://127.0.0.1:8765/v1"))
                    $0.finish()
                })
            }
        }
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.setUpThisMac()
        await model.voiceSetup.settled()
        #expect(model.voiceSetup.seenSteps == ["voice_llm", "install"])
    }
}
