import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// The model server's start, the speech model download and This Mac's Set Up… as the
/// screens show them: the steps each run has reached, refusals said as guidance, and the
/// question quitting asks while one runs.
@MainActor @Suite struct LocalSetupTests {
    func states(_ steps: [RunStep]) -> [RunStep.State] { steps.map(\.state) }

    // MARK: - Steps

    @Test func stepsBeforeTheOneUnderWayAreDoneAndTheRestToCome() {
        let order = RunSteps.modelServer
        #expect(states(RunSteps.steps(order, reached: "start", stage: .running)) == [.done, .done, .current, .pending])
        #expect(states(RunSteps.steps(order, reached: "unit", stage: .failed)) == [.done, .failed, .pending, .pending])
        #expect(states(RunSteps.steps(order, reached: "check", stage: .finished)) == [.done, .done, .done, .done])
        #expect(states(RunSteps.steps(order, reached: "", stage: .notStarted)).allSatisfy { $0 == .pending })
        // Before the first step, or on a step this build does not know: the first is under way.
        #expect(states(RunSteps.steps(order, reached: "", stage: .running)) == [.current, .pending, .pending, .pending])
        #expect(states(RunSteps.steps(order, reached: "new", stage: .running)).first == .current)
    }

    @Test func aSkippedStepIsDoneByBeingPassed() {
        // Speech models already downloaded: the CLI goes from install to configure.
        let steps = RunSteps.steps(RunSteps.voice, reached: "configure", stage: .running)
        #expect(steps.map(\.id) == ["install", "download", "configure", "unit", "start"])
        #expect(states(steps) == [.done, .done, .current, .pending, .pending])
    }

    @Test func eachCommandsStepsAreTheCLIs() {
        #expect(RunSteps.modelServer.map(\.id) == ["runtime", "unit", "start", "check"])
        #expect(RunSteps.voice.map(\.id) == ["install", "download", "configure", "unit", "start"])
        #expect(RunSteps.voiceDownload.map(\.id) == ["install", "download"])
    }

    @Test func aDownloadSaysItsSizeWithOrWithoutATotal() {
        #expect(RunSteps.sizes(done: 1_363_148_800, total: 2_726_297_600)
            == ModelRow.progress(done: 1_363_148_800, total: 2_726_297_600))
        #expect(RunSteps.sizes(done: 9_437_184, total: nil) == "\(Formatting.bytes(9_437_184)) so far")
        #expect(RunSteps.sizes(done: 0, total: nil) == nil)
    }

    // MARK: - This Mac's two runs as one list

    @Test func whileTheServerStartsTheVoiceIsAllToCome() {
        let activity = StreamRun<ModelServerStarted>.Activity(
            step: "runtime", message: "Installing llama.cpp with Homebrew",
            progress: .init(done: 9_437_184, total: nil))
        let state = ThisMacProgress.state(withServer: true, server: .running(activity), serverStep: "runtime",
                                          setup: nil, setupStep: "")
        #expect(state.groups.map(\.title) == ["Model Server", "Voice Server"])
        #expect(states(state.groups[0].steps) == [.current, .pending, .pending, .pending])
        #expect(states(state.groups[1].steps).allSatisfy { $0 == .pending })
        #expect(state.message == "Installing llama.cpp with Homebrew")
        #expect(state.progress?.done == 9_437_184 && state.progress?.total == nil)
        #expect(state.ending == .running && !state.stopping)
    }

    @Test func onceTheVoiceBeginsTheServerIsDone() {
        let state = ThisMacProgress.state(
            withServer: true, server: .finished(ModelServerStarted(url: "u", servedModel: "m")), serverStep: "check",
            setup: .running(step: "download", message: "Downloading", progress: .init(done: 5, total: 10)),
            setupStep: "download")
        #expect(states(state.groups[0].steps).allSatisfy { $0 == .done })
        #expect(states(state.groups[1].steps) == [.done, .current, .pending, .pending, .pending])
        #expect(state.message == "Downloading")
        #expect(state.progress?.done == 5 && state.progress?.total == 10)
    }

    @Test func aSizeTheSourceDidNotSayIsNoTotal() {
        let state = ThisMacProgress.state(withServer: false, server: nil, serverStep: "",
                                          setup: .running(step: "download", message: "", progress: .init(done: 5, total: 0)),
                                          setupStep: "download")
        #expect(state.groups.count == 1 && state.groups[0].title == nil)
        #expect(state.progress?.total == nil)
    }

    @Test func aFailureEndsTheListWhereverItHappened() {
        let server = ThisMacProgress.state(withServer: true, server: .failed("brew install llama.cpp did not finish"),
                                           serverStep: "runtime", setup: nil, setupStep: "")
        #expect(server.ending == .failed("brew install llama.cpp did not finish"))
        #expect(states(server.groups[0].steps) == [.failed, .pending, .pending, .pending])

        let voice = ThisMacProgress.state(withServer: false, server: nil, serverStep: "",
                                          setup: .failed("no room"), setupStep: "unit")
        #expect(voice.ending == .failed("no room"))
        #expect(states(voice.groups[0].steps) == [.done, .done, .done, .failed, .pending])

        let stopping = ThisMacProgress.state(withServer: true, server: .cancelling, serverStep: "unit",
                                             setup: nil, setupStep: "")
        #expect(stopping.stopping && stopping.ending == .running)

        let finished = ThisMacProgress.state(withServer: false, server: nil, serverStep: "",
                                             setup: .finished(baseURL: "http://127.0.0.1:8765/v1"), setupStep: "start")
        #expect(finished.ending == .finished("http://127.0.0.1:8765/v1"))
    }

    // MARK: - Refusals are guidance

    @Test func theRefusalsThatAreAStepToTakeFirstAreKnown() {
        #expect(LocalRefusal.of("set up a local model first: keryx models use NAME") == .needsModel)
        #expect(LocalRefusal.of("choose and download a model first: LLM_SERVER_MODEL is not set") == .needsModel)
        #expect(LocalRefusal.of("The local model server isn't running: start it first.") == .needsServer)
        #expect(LocalRefusal.of("brew install llama.cpp did not finish") == nil)
        #expect(ThisMacProgress.refused(.failed("The local model server isn't running: start it first.")))
        #expect(!ThisMacProgress.refused(.failed("no room")))
        #expect(!ThisMacProgress.refused(.running))
    }

    @Test func noLocalModelPointsAtTheRecommendedOne() {
        let recommended = PreviewData.models.models.first { $0.recommended }!
        #expect(VoiceWording.needsModelDetail(recommended: recommended).contains(
            "\(recommended.title), recommended for this Mac, is downloaded: choose Use This Model."))
        var fresh = recommended
        fresh.downloaded = false
        #expect(VoiceWording.needsModelDetail(recommended: fresh).contains("download it, then choose Use This Model"))
        #expect(VoiceWording.needsModelButton(recommended: recommended) == "Show Recommended Model")
        #expect(VoiceWording.needsModelButton(recommended: nil) == "Go to Local Models")
    }

    @Test func theQuestionSaysWhenTheModelServerStartsFirst() {
        #expect(VoiceWording.setUpMessage(startsServer: true).hasPrefix("The model server isn't running, so Keryx starts it first"))
        #expect(VoiceWording.setUpMessage(startsServer: false).hasPrefix("Keryx installs a voice server"))
        var report = PreviewData.models
        #expect(VoiceWording.wordsServer(report)?.running == true)
        report.servers.llm.reachable = false
        #expect(VoiceWording.wordsServer(report)?.text == "Model server not running — Set Up starts it")
        report.servers.llm.model = nil
        #expect(VoiceWording.wordsServer(report) == nil, "Ollama's, or elsewhere: not this Mac's to start")
    }

    @Test func theModelServerSaysWhyItCannotStart() {
        var servers = PreviewData.models.servers
        #expect(ModelsWording.cannotStart(servers) == nil)
        servers.llm.model = nil
        servers.localAgent.baseUrl = "http://127.0.0.1:11434/v1"
        #expect(ModelsWording.cannotStart(servers)
            == "The local agent's model is served at 127.0.0.1:11434, not by this Mac's model server.")
        servers.localAgent.model = nil
        #expect(ModelsWording.cannotStart(servers) == "Choose a model below first.")
        #expect(ModelsWording.serverState(PreviewData.models.servers.llm) == "Running at 127.0.0.1:8090")
    }

    @Test func showingTheRecommendedModelGoesToLocalModels() {
        let model = AppModel.preview()
        model.showRecommendedModel()
        #expect(model.selection == .models)
        #expect(model.modelsSpotlight == "qwen3-coder-30b-a3b")
        model.show(inModels: ModelsAnchor.voiceModels)
        #expect(model.modelsSpotlight == ModelsAnchor.voiceModels)
    }

    // MARK: - Quitting

    @Test func quittingNamesAModelServerStartAndASpeechModelDownload() {
        #expect(Quitting.question(downloading: false, settingUpVoice: false, startingModelServer: true,
                                  assistant: "Lyra")
            == .init(message: "The model server is starting. Quit anyway?",
                     detail: "It stops, and its service is removed. Start Model Server in Local Models starts it again."))
        #expect(Quitting.question(downloading: false, settingUpVoice: false, downloadingVoiceModels: true,
                                  assistant: "Lyra")?.message == "The speech models are downloading. Quit anyway?")
        #expect(Quitting.question(downloading: true, settingUpVoice: true, assistant: "Lyra")?.detail
            == "Both stop. The download picks up where it left off, and Set Up… in Voice starts the setup again.")
        let three = Quitting.question(downloading: true, settingUpVoice: false, startingModelServer: true,
                                      downloadingVoiceModels: true, assistant: nil)
        #expect(three?.message
            == "A model is downloading, the model server is starting and the speech models are downloading. Quit anyway?")
        #expect(three?.detail.hasPrefix("They all stop. The download picks up where it left off, ") == true)
    }
}
