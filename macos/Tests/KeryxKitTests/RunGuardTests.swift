import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview

/// The runs that must not overlap, refused by the model itself — so the Setup Assistant,
/// Local Models, Voice, the Use Model sheet and the menu are all held to it at once: a
/// speech model download beside a voice setup (which fetches them too), a model server
/// started or stopped under a voice setup, two stops, and a start during a stop.
@MainActor
@Suite struct RunGuardTests {
    func makeModel(client: StubClient) async -> AppModel {
        let model = AppModel(locator: StubLocator(), makeClient: { _ in client },
                             preferences: Preferences(defaults: freshDefaults()),
                             timing: AppModelTests.noPoll)
        await model.start()
        await model.refresh(.models)
        return model
    }

    // MARK: - Speech models beside a voice setup

    @Test func noSpeechModelDownloadWhileTheVoiceIsBeingSetUp() async {
        let setup = ThisMacSetupTests.ManualSetup()
        let client = StubClient { $0.voiceHere = { _ in setup.stream() } }
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.setUpThisMac()
        #expect(model.voiceSetup.isRunning)
        #expect(!model.canDownloadVoiceModels)
        #expect(!model.downloadVoiceModels())
        #expect(!client.calls.contains("voice download"))
        setup.finish()
        await model.voiceSetup.settled()
    }

    @Test func noSpeechModelDownloadWhileTheSetupWaitsForItsModelServer() async {
        let server = ManualRun<ModelServerStarted>()
        let client = StubClient {
            $0.models = .success(ThisMacSetupTests.serverDown)
            $0.modelServer = { _ in server.stream() }
        }
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.setUpThisMac()
        #expect(model.modelServerRun.isRunning && !model.voiceSetup.isBusy, "the voice waits for the server")
        #expect(!model.canDownloadVoiceModels)
        #expect(!model.downloadVoiceModels())
        #expect(!client.calls.contains("voice download"))
        model.cancelModelServer()
        server.finish()
        await model.modelServerRun.settled()
        #expect(model.canDownloadVoiceModels, "the chain is over")
    }

    @Test func aVoiceSetupReachedWhileTheSpeechModelsDownloadIsRefusedOutLoud() async {
        let download = ManualRun<VoiceModelsDownloaded>()
        let client = StubClient { $0.voiceDownload = { download.stream() } }
        let model = await makeModel(client: client)
        defer { model.stop() }
        #expect(model.downloadVoiceModels())
        // What a model server's `done` goes on to.
        model.setUpVoiceHere()
        #expect(model.voiceSetup.phase == .failed(AppModel.speechModelsDownloading))
        #expect(!client.calls.contains { $0.hasPrefix("voice use here") })
        download.finish()
        await model.voiceDownloadRun.settled()
    }

    // MARK: - The model server under a voice setup

    @Test func noModelServerStartWhileTheVoiceIsBeingSetUp() async {
        let setup = ThisMacSetupTests.ManualSetup()
        let client = StubClient { $0.voiceHere = { _ in setup.stream() } }
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.setUpThisMac()
        let started = model.thisMacSetup
        #expect(started != nil)
        #expect(!model.canStartModelServer)
        #expect(!model.runModelServer())
        #expect(!client.calls.contains { $0.hasPrefix("models start") })
        #expect(model.thisMacSetup == started, "the setup under way is still the one Cancel stops")
        model.cancelThisMacSetup()
        #expect(model.voiceSetup.phase == .cancelling)
        await model.voiceSetup.settled()
    }

    @Test func theModelServerIsNotStoppedUnderAVoiceSetupNorItsOwnStart() async {
        let setup = ThisMacSetupTests.ManualSetup()
        let server = ManualRun<ModelServerStarted>()
        let client = StubClient {
            $0.voiceHere = { _ in setup.stream() }
            $0.modelServer = { _ in server.stream() }
        }
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.setUpThisMac()
        #expect(await model.stopModelServer() == .failure(.refused(AppModel.modelServerInUse)))
        setup.finish()
        await model.voiceSetup.settled()
        model.voiceSetup.dismiss()

        #expect(model.runModelServer())
        #expect(await model.stopModelServer() == .failure(.refused(AppModel.modelServerInUse)))
        #expect(!client.calls.contains("models stop"))
        server.finish()
        await model.modelServerRun.settled()
    }

    // MARK: - Stopping the model server

    @Test func whileTheModelServerStopsNothingStartsItAndNoSecondStopRuns() async {
        let gate = OptionsGate()
        let client = StubClient { $0.stopWait = { await gate.wait() } }
        let model = await makeModel(client: client)
        defer { model.stop() }
        let stopping = Task { await model.stopModelServer() }
        #expect(await eventually { gate.held == 1 })
        #expect(model.stoppingModelServer)
        #expect(model.isBusy, "quitting waits for it")
        #expect(!model.canStartModelServer)
        let mark = client.calls.count
        #expect(!model.runModelServer())
        model.setUpThisMac(startServer: true)
        #expect(model.thisMacSetup == nil)
        #expect(await model.stopModelServer() == .failure(.refused(AppModel.modelServerStopping)))
        #expect(Array(client.calls.dropFirst(mark)).isEmpty, "nothing more was run")
        gate.open()
        _ = await stopping.value
        #expect(!model.stoppingModelServer)
        #expect(model.canStartModelServer)
    }

    // MARK: - Short writes, for quitting

    @Test func aWriteIsListedWhileItRunsAndQuittingCanWaitForIt() async {
        let gate = OptionsGate()
        let client = StubClient { $0.stopWait = { await gate.wait() } }
        let model = await makeModel(client: client)
        defer { model.stop() }
        #expect(model.writing.isEmpty)
        let stopping = Task { await model.stopModelServer() }
        #expect(await eventually { gate.held == 1 })
        #expect(model.writing == [.modelServerStop])
        #expect(await model.writesFinish(within: .milliseconds(100)) == false, "still going at the limit")
        gate.open()
        #expect(await model.writesFinish(within: .seconds(10)))
        _ = await stopping.value
        #expect(model.writes.isEmpty)
    }

    @Test func settledWritesAndReadsLeaveNothingListed() async {
        let model = await makeModel(client: StubClient())
        defer { model.stop() }
        // Settled, a write leaves nothing listed; failed or not.
        _ = await model.set(key: "ASSISTANT_NAME", to: "Vega")
        _ = await model.setPin("482915", replace: false)
        _ = await model.applyPhoneWebhooks()
        #expect(model.writes.isEmpty)
        // A read is never one.
        _ = await model.freshPhoneWebhooks()
        _ = await model.verify(key: "OPENAI_API_KEY", value: "sk-x")
        #expect(model.writes.isEmpty)
        #expect(Set(AppModel.Write.allCases.map(\.clause)).count == AppModel.Write.allCases.count)
    }
}
