import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview

/// The setup plan, the PIN, the phone, the service, sign-ins and plugins, as `AppModel`
/// holds them: what each action asks of Keryx, and what it reads again after.
@MainActor
@Suite struct AppModelSetupTests {
    func makeModel(client: StubClient = StubClient()) async -> AppModel {
        let model = AppModel(locator: StubLocator(), makeClient: { _ in client },
                             preferences: Preferences(defaults: freshDefaults()),
                             timing: AppModelTests.noPoll)
        await model.start()
        return model
    }

    /// What the model asked for after `mark` (the calls made since).
    func calls(_ client: StubClient, after mark: Int) -> [String] {
        Array(client.calls.dropFirst(mark))
    }

    @Test func refreshAllLoadsTheNewDocumentsToo() async {
        let client = StubClient()
        let model = await makeModel(client: client)
        defer { model.stop() }
        await model.refresh(.all)
        #expect(model.setupPlan.value == PreviewData.setupPlanPartway)
        #expect(model.pin.value == PreviewData.pinFile)
        #expect(model.service.value == PreviewData.service)
        #expect(model.phoneNumbers.value == PreviewData.phoneNumbers)
        #expect(model.phoneWebhooks.value == PreviewData.phoneWebhooks)
        #expect(model.phoneTunnel.value == PreviewData.phoneTunnel)
        #expect(model.integrations.value == PreviewData.integrations)
        #expect(model.plugins.value == PreviewData.plugins)
        #expect(model.tools.value == PreviewData.tools)
        #expect(model.sshHosts.value == PreviewData.sshHosts)
    }

    @Test func aFailedDocumentKeepsItsError() async {
        let client = StubClient { $0.phoneNumbers = .failure(.timedOut(command: "keryx phone numbers")) }
        let model = await makeModel(client: client)
        defer { model.stop() }
        await model.refreshPhone()
        #expect(model.phoneNumbers.error == .timedOut(command: "keryx phone numbers"))
        #expect(model.phoneWebhooks.value == PreviewData.phoneWebhooks)
        #expect(model.phoneTunnel.value == PreviewData.phoneTunnel)
    }

    @Test func relocatingForgetsTheNewDocuments() async {
        let model = await makeModel()
        defer { model.stop() }
        await model.refresh(.setupPlan, .plugins)
        #expect(model.setupPlan.value != nil)
        model.stop()
        let locating = Task { await model.relocate() }
        await locating.value
        // Found again, only the status and the doctor are read back.
        #expect(model.plugins.value == nil)
        #expect(model.setupPlan.value == nil)
    }

    @Test func markingAStepReadsThePlanAgain() async {
        let client = StubClient()
        let model = await makeModel(client: client)
        defer { model.stop() }
        let mark = client.calls.count
        #expect(await model.markSetupStep("local") == nil)
        #expect(calls(client, after: mark) == ["setup mark local", "setup plan"])
    }

    @Test func settingThePinReadsItAndThePlanAgainAndAsksForARestart() async {
        let client = StubClient()
        let model = await makeModel(client: client)
        defer { model.stop() }
        let mark = client.calls.count
        let result = await model.setPin("482915", replace: true)
        #expect(try! result.get().restartNeeded)
        #expect(model.configNeedsRestart)
        let asked = calls(client, after: mark)
        #expect(asked.first == "pin set --stdin --replace")
        #expect(Set(asked.dropFirst()) == ["pin", "setup plan"])
        #expect(!asked.joined().contains("482915"))
    }

    @Test func aRefusedPinIsReturnedAndNothingIsReadAgain() async {
        let refusal = KeryxError.failed(command: "keryx pin set", exitCode: 1, message: "a PIN is already set")
        let client = StubClient { $0.pinSet = .failure(refusal) }
        let model = await makeModel(client: client)
        defer { model.stop() }
        let mark = client.calls.count
        #expect(await model.setPin("482915", replace: false) == .failure(refusal))
        #expect(calls(client, after: mark) == ["pin set --stdin"])
        #expect(!model.configNeedsRestart)
    }

    @Test func verifyingSavesNothingAndReadsNothingAgain() async {
        let client = StubClient { $0.verify = .success(PreviewClient.notVerified) }
        let model = await makeModel(client: client)
        defer { model.stop() }
        let mark = client.calls.count
        #expect(await model.verify(key: "OPENAI_API_KEY", value: "sk-x") == .success(PreviewClient.notVerified))
        _ = await model.verify(key: "OPENAI_API_KEY")
        _ = await model.verifyTwilio(accountSID: "AC1", authToken: "t")
        #expect(calls(client, after: mark) == ["config verify OPENAI_API_KEY --stdin", "config verify OPENAI_API_KEY",
                                               "config verify TWILIO_AUTH_TOKEN --stdin --pair"])
        #expect(!model.configNeedsRestart)
    }

    @Test func applyingTheWebhooksReadsThePhoneAndThePlanAgain() async {
        let client = StubClient()
        let model = await makeModel(client: client)
        defer { model.stop() }
        let mark = client.calls.count
        #expect(await model.applyPhoneWebhooks() == .success(WebhooksApplied(applied: true, number: "+15550001111")))
        let asked = calls(client, after: mark)
        #expect(asked.first == "phone webhooks --apply")
        #expect(Set(asked.dropFirst()) == ["phone webhooks", "phone numbers", "setup plan"])
    }

    @Test func theServiceInstallersReadTheUnitsAgain() async {
        let client = StubClient()
        let model = await makeModel(client: client)
        defer { model.stop() }
        var mark = client.calls.count
        _ = await model.installService()
        #expect(calls(client, after: mark).first == "service install")
        #expect(calls(client, after: mark).contains("service"))
        mark = client.calls.count
        _ = await model.uninstallService(llm: true)
        #expect(calls(client, after: mark).first == "service uninstall --llm")
        mark = client.calls.count
        _ = await model.stopModelServer()
        #expect(calls(client, after: mark).first == "models stop")
        #expect(calls(client, after: mark).contains("models list"))
    }

    @Test func aFailedInstallReadsNothingAgain() async {
        let failure = KeryxError.failed(command: "keryx service install", exitCode: 2, message: "no checkout")
        let client = StubClient { $0.serviceResult = .failure(failure) }
        let model = await makeModel(client: client)
        defer { model.stop() }
        let mark = client.calls.count
        #expect(await model.installService() == .failure(failure))
        #expect(calls(client, after: mark) == ["service install"])
    }

    @Test func pluginChangesReadThePluginsToolsAndPlanAgain() async {
        let client = StubClient()
        let model = await makeModel(client: client)
        defer { model.stop() }
        for (change, call) in [
            ({ await model.installPlugin("check_billing", values: PluginValues(settings: ["provider": "openai"])) },
             "plugins install check_billing"),
            ({ await model.setPlugin("check_billing", values: PluginValues(settings: ["monthly_budget": "40"])) },
             "plugins set check_billing"),
            ({ await model.removePlugin("check_billing") }, "plugins remove check_billing"),
        ] as [(() async -> Result<PluginResult, KeryxError>, String)] {
            let mark = client.calls.count
            _ = await change()
            let asked = calls(client, after: mark)
            #expect(asked.first == call)
            #expect(Set(asked.dropFirst()) == ["plugins list", "tools", "setup plan"])
        }
    }

    @Test func signingOutReadsTheAccountsAgain() async {
        let client = StubClient()
        let model = await makeModel(client: client)
        defer { model.stop() }
        let mark = client.calls.count
        _ = await model.signOut(.googleWorkspace)
        let asked = calls(client, after: mark)
        #expect(asked.first == "auth logout google-workspace")
        #expect(Set(asked.dropFirst()) == ["integrations", "plugins list", "tools", "setup plan"])
    }

    // MARK: - The streams

    @Test func aSignInRunsOneAtATimeAndAStoredTokenWantsARestart() async {
        let manual = ManualRun<SignedIn>()
        let client = StubClient { $0.signIn = { _, _ in manual.stream() } }
        let model = await makeModel(client: client)
        defer { model.stop() }
        #expect(model.signIn(.claude))
        #expect(model.signingIn == .claude)
        #expect(!model.signIn(.codex), "one sign-in at a time")
        #expect(model.signingIn == .claude)
        #expect(model.isBusy)
        manual.send(.code)
        #expect(await eventually { model.signInRun.activity?.wantsCode == true })
        #expect(model.signInRun.send("c#s"))
        #expect(manual.sent == ["c#s"])
        manual.send(.done(SignedIn(name: "claude", message: "Signed in.", stored: "CLAUDE_CODE_OAUTH_TOKEN")))
        manual.finish()
        await model.signInRun.settled()
        #expect(model.configNeedsRestart)
        #expect(await eventually { client.calls.contains("integrations") && client.calls.contains("setup plan") })
        #expect(client.calls.filter { $0.hasPrefix("auth login") } == ["auth login claude"])
        #expect(!model.isBusy)
    }

    @Test func aGoogleSignInPassesItsClientFile() async {
        let client = StubClient()
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.signIn(.gmail, clientFile: "/Users/you/Downloads/client.json")
        await model.signInRun.settled()
        #expect(client.calls.contains("auth login gmail --client-file"))
    }

    @Test func startingTheModelServerReadsTheModelsOnceItServes() async {
        let manual = ManualRun<ModelServerStarted>()
        let client = StubClient { $0.modelServer = { _ in manual.stream() } }
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.runModelServer(runtime: "ollama")
        #expect(client.calls.last == "models start --runtime ollama")
        #expect(model.modelServerRun.isRunning)
        manual.send(.done(ModelServerStarted(url: "http://127.0.0.1:8090/v1", servedModel: "m")))
        manual.finish()
        await model.modelServerRun.settled()
        #expect(model.modelServerRun.result?.servedModel == "m")
        #expect(await eventually { client.calls.contains("models list") && client.calls.contains("service") })
    }

    @Test func downloadingTheSpeechModelsReadsTheVoiceAgain() async {
        let manual = ManualRun<VoiceModelsDownloaded>()
        let client = StubClient { $0.voiceDownload = { manual.stream() } }
        let model = await makeModel(client: client)
        defer { model.stop() }
        let mark = client.calls.count
        model.downloadVoiceModels()
        manual.send(.done(VoiceModelsDownloaded(stt: "parakeet-tdt", tts: "kokoro")))
        manual.finish()
        await model.voiceDownloadRun.settled()
        #expect(await eventually { self.calls(client, after: mark) == ["voice download", "voice"] })
    }

    @Test func cancelRunsStopsEveryStreamRun() async {
        let start = ManualRun<ModelServerStarted>()
        let download = ManualRun<VoiceModelsDownloaded>()
        let signIn = ManualRun<SignedIn>()
        let client = StubClient {
            $0.modelServer = { _ in start.stream() }
            $0.voiceDownload = { download.stream() }
            $0.signIn = { _, _ in signIn.stream() }
        }
        let model = await makeModel(client: client)
        defer { model.stop() }
        model.runModelServer()
        model.downloadVoiceModels()
        model.signIn(.codex)
        model.cancelRuns()
        #expect(start.stopRequests == 1 && download.stopRequests == 1 && signIn.stopRequests == 1)
        #expect(model.isBusy, "still stopping")
        for run in [start.finish, download.finish, signIn.finish] { run(nil) }
        await model.modelServerRun.settled()
        await model.voiceDownloadRun.settled()
        await model.signInRun.settled()
        #expect(!model.isBusy)
    }

    @Test func withNoKeryxNothingStarts() async {
        let model = AppModel(locator: StubLocator(.failure(LocateFailure(lookedAt: []))),
                             makeClient: { _ in StubClient() }, preferences: Preferences(defaults: freshDefaults()),
                             timing: AppModelTests.noPoll)
        await model.start()
        defer { model.stop() }
        #expect(!model.signIn(.claude))
        model.runModelServer()
        model.downloadVoiceModels()
        #expect(model.modelServerRun.phase == nil && model.voiceDownloadRun.phase == nil)
        if case .failure(.notFound) = await model.installService() {} else { Issue.record("not refused") }
    }
}
