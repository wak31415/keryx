import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
import KeryxTestSupport
@testable import KeryxUI

/// The voice LLM on the screens that show it: Voice's LLM picker, Local Models' Voice LLM
/// Server and memory bar, Use This Model's question, the Agents tab's Local Agent section,
/// and the refusals said as guidance.
@MainActor @Suite struct VoiceLLMScreenTests {
    @Test func catalogHasEveryVoiceLLMState() {
        let ids = Set(ScreenCatalog.entries().map(\.id))
        for id in ["voice-here-own-llm", "voice-here-llm-own-picked", "voice-here-llm-remote-picked",
                   "voice-here-llm-choosing", "voice-here-llm-chosen", "voice-here-llm-live-call",
                   "voice-here-llm-failed", "voice-here-needs-voice-llm", "voice-here-live-call",
                   "models-voice-llm-running", "models-voice-llm-starting", "models-voice-llm-refused",
                   "models-voice-llm-choosing", "models-use-sheet-voice"] {
            #expect(ids.contains(id), "missing \(id)")
        }
    }

    // MARK: - The picker

    @Test func theSourcesAreSaidAsTheOwnerReadsThem() {
        #expect(VoiceLLMSource.allCases.map(VoiceLLMWording.title)
            == ["Same as Local Agent", "On This Mac", "Remote or Cloud"])
        #expect(VoiceLLMSource.allCases.map(VoiceLLMWording.primary)
            == ["Use Local Agent's Model", "Use This Model", "Check and Use"])
        #expect(VoiceLLMWording.caption(.shared, inUse: .shared) == "The model that puts the replies together")
        #expect(VoiceLLMWording.caption(.here, inUse: .shared)
            == "Same as Local Agent stays in use until you choose Use This Model")
    }

    @Test func aVoiceModelIsMarkedOnlyByItsOwnFitAsTheVoicesLLM() throws {
        let models = PreviewData.models.models
        let choices = VoiceLLMWording.choices(models, current: nil)
        #expect(choices.count == models.count)
        #expect(choices.map(\.needsGb) == choices.map(\.needsGb).sorted())
        // `fits` and `fits_with_voice_llm` are the agent's: neither marks a voice model.
        // `fits_as_voice_llm` now comes from the fixture too (the CLI prints it for real);
        // an older CLI that never sent it is nil, which still marks nothing.
        var small = try #require(models.first { $0.name == "qwen3.5-4b" })
        #expect(small.fits && small.fitsWithVoiceLlm == true)
        small.fitsAsVoiceLlm = nil
        #expect(VoiceLLMWording.menuTitle(small, recommended: "qwen3.5-4b") == "Qwen3.5 4B — Recommended")
        #expect(!VoiceLLMWording.hereCaption(small).contains("fit"))
        // `fits_as_voice_llm`, once the CLI prints it, lights it up (the catalog's hand-edited
        // `voice-here-llm-fits`).
        var fits = small
        fits.fitsAsVoiceLlm = true
        #expect(VoiceLLMWording.menuTitle(fits, recommended: nil) == "Qwen3.5 4B — Fits")
        #expect(VoiceLLMWording.hereCaption(fits).hasSuffix(", and fits"))
        fits.fitsAsVoiceLlm = false
        #expect(VoiceLLMWording.menuTitle(fits, recommended: nil) == "Qwen3.5 4B")
        #expect(VoiceLLMWording.hereCaption(fits).hasSuffix(", more than is free"))
        #expect(VoiceLLMWording.hereCaption(nil) == "A small model of its own answers fastest")
        #expect(Set(ScreenCatalog.entries().map(\.id)).contains("voice-here-llm-fits"))
    }

    @Test func remoteDraftsAreKeptInTheAppsOwnPreferencesNeverKeryxsSettings() async {
        let fake = FakeRunner()
        let defaults = TestDefaults.fresh()
        let model = AppModel(locator: PreviewLocator(), makeClient: { CLIClient(location: $0, runner: fake) },
                             preferences: Preferences(defaults: defaults), timing: AppModel.Timing(poll: nil))
        await model.start()
        let mark = fake.requests.count
        model.keepVoiceLLMDraft(address: " https://api.openai.com/v1 ", model: "gpt-5-mini")
        #expect(model.voiceLLMDraft.address == "https://api.openai.com/v1")
        #expect(model.voiceLLMDraft.model == "gpt-5-mini")
        #expect(fake.requests.count == mark, "nothing is asked of Keryx: no VOICE_LLM_BASE_URL, no VOICE_LLM_MODEL")
        // Each field on its own; a blank one is forgotten.
        model.keepVoiceLLMDraft(address: "", model: "gpt-5-mini")
        #expect(model.voiceLLMDraft.address == nil && model.voiceLLMDraft.model == "gpt-5-mini")
        #expect(Preferences(defaults: defaults).voiceLLMDraftModel == "gpt-5-mini", "kept across launches")
    }

    @Test func aStoppedVoiceLLMChoiceSaysStoppingUntilItEnds() async {
        let model = AppModel.preview(client: PreviewClient(voiceLLM: .slowToStop([
            .step(step: "download", message: "Downloading Qwen3.5 4B for the voice LLM (2.7 GB)")])))
        #expect(model.chooseVoiceLLM(.here(model: "qwen3.5-4b")))
        #expect(await Waiting.until { model.voiceLLMRun.lastStep == "download" })
        model.voiceLLMRun.cancel()
        #expect(model.voiceLLMRun.phase == .cancelling)
        guard case let .running(progress)? = RunDisplay.of(model.voiceLLMRun) else {
            Issue.record("not shown as running")
            return
        }
        #expect(progress.stopping, "Stopping…, its Cancel disabled")
        #expect(model.voiceLLMBusy && !model.canChangeVoiceLLM, "nothing else starts meanwhile")
        #expect(VoiceLLMWording.stopping.hasPrefix("Stopping…"))
    }

    @Test func theWorkBesideAVoiceOfItsOwnIsOfferedWhatLeavesRoomForIt() {
        let report = PreviewData.models
        #expect(ModelsRules.fitting(report).map(\.name).contains("qwen3.8-27b"))
        let beside = ModelsRules.fitting(report, ownVoice: true).map(\.name)
        #expect(!beside.contains("qwen3.8-27b") && !beside.contains("qwen3-coder-30b-a3b"))
        #expect(beside.contains("gpt-oss-20b"))
        #expect(ModelsRules.voiceChoices(report).count == report.models.count)
    }

    @Test func whatIsInUseIsTheSameSourceModelAndAWorkingServer() {
        let own = PreviewData.voiceOwnLLM
        #expect(VoiceLLMWording.inUse(.here(model: "qwen3.5-4b"), voice: own))
        #expect(!VoiceLLMWording.inUse(.here(model: "qwen3.5-9b"), voice: own))
        var down = own
        down.llm?.reachable = false
        #expect(!VoiceLLMWording.inUse(.here(model: "qwen3.5-4b"), voice: down), "Use This Model starts it")
        #expect(VoiceLLMWording.inUse(.shared, voice: PreviewData.voice))
        #expect(!VoiceLLMWording.inUse(.shared, voice: own))
        var remote = PreviewData.voiceHere
        remote.llm = VoiceLLMStatus(source: "remote", model: "gpt-5-mini", url: "https://api.openai.com/v1",
                                    reachable: true, unitInstalled: false)
        #expect(VoiceLLMWording.inUse(.remote(url: "https://api.openai.com", model: "gpt-5-mini", key: .stored),
                                      voice: remote))
        #expect(!VoiceLLMWording.inUse(.remote(url: "https://api.openai.com", model: "gpt-5-mini", key: .typed("k")),
                                       voice: remote), "a new key is a change")
        #expect(!VoiceLLMWording.inUse(.remote(url: "https://api.openai.com", model: "gpt-5-mini", key: .clear),
                                       voice: remote))
    }

    @Test func theSharedChoiceWantsALocalAgentModel() {
        #expect(VoiceLLMWording.cannot(.shared, report: PreviewData.models, voice: PreviewData.voice) == nil)
        #expect(VoiceLLMWording.cannot(.shared, report: PreviewData.modelsEmptyServers, voice: PreviewData.voice)
            == "Choose a local model in Local Models first.")
        #expect(VoiceLLMWording.cannot(.here, report: PreviewData.modelsEmptyServers, voice: PreviewData.voice) == nil)
    }

    @Test func theServersStateAndAddressesAreSaidPlainly() throws {
        let own = try #require(PreviewData.voiceOwnLLM.llm)
        #expect(VoiceLLMWording.serverState(own) == "Running at 127.0.0.1:8091")
        var down = own
        down.reachable = false
        #expect(VoiceLLMWording.serverState(down) == "Not serving its model")
        down.unitInstalled = false
        #expect(VoiceLLMWording.serverState(down) == "Not running")
        #expect(VoiceLLMWording.remoteState(VoiceLLMStatus(source: "remote", model: "m", url: "u", reachable: nil,
                                                           unitInstalled: false)) == nil, "not asked")
        #expect(VoiceLLMWording.sameAddress("https://api.openai.com/v1/", "https://API.openai.com"))
        #expect(!VoiceLLMWording.sameAddress("http://a:1", "http://b:1"))
    }

    @Test func aChoiceThatWorkedSaysWhereTheWordsComeFromAndWhatBecameOfTheVoice() {
        let models = PreviewData.models.models
        let here = VoiceLLMWording.finished(VoiceLLMChosen(source: "here", model: "qwen3.5-4b", url: nil,
                                                           voiceRestarted: true, voiceRestartNeeded: false),
                                            models: models)
        #expect(here.title == "The voice now uses Qwen3.5 4B on this Mac")
        #expect(here.subtitle == "The voice server was restarted onto it, and answers.")
        let shared = VoiceLLMWording.finished(VoiceLLMChosen(source: "shared", model: nil, url: nil,
                                                             voiceRestarted: false, voiceRestartNeeded: true),
                                              models: models)
        #expect(shared.title == "The voice now uses the local agent's model")
        #expect(shared.subtitle == "The voice server takes it up when it starts.")
        let remote = VoiceLLMWording.finished(VoiceLLMChosen(source: "remote", model: "gpt-5-mini",
                                                             url: "https://api.openai.com/v1", voiceRestarted: false,
                                                             voiceRestartNeeded: false), models: models)
        #expect(remote.title == "The voice now uses gpt-5-mini at api.openai.com")
        #expect(remote.subtitle == nil)
    }

    // MARK: - Refusals as guidance

    @Test func aLiveCallIsAWaitNotAFailure() {
        let llm = StreamFailure("A call is going on, and switching the voice's words restarts the voice server, "
            + "which would cut it off: try again once the line is clear.", detail: "1 live call(s)")
        #expect(LocalRefusal.of(llm) == .liveCall)
        // By the count alone, whatever a later CLI's sentence.
        #expect(LocalRefusal.of(StreamFailure("Busy.", detail: "3 live call(s)")) == .liveCall)
        #expect(LocalRefusal.of(StreamFailure("Busy.", detail: "no answer")) == nil)
        #expect(VoiceLLMWording.guidanceTitle(.liveCall, assistant: "Lyra") == "Lyra is on a call")
        #expect(VoiceLLMWording.onACall("Lyra") == "Lyra is on a call. Try again when it ends.")
        let setUp = StreamFailure("A call is going on, and setting up the voice here restarts its server, which would "
            + "cut it off: try again once the line is clear.", detail: "2 live call(s)")
        #expect(LocalRefusal.of(setUp) == .liveCall, "Checking's voice row says it as a wait")
        #expect(VoiceLLMWording.guidanceDetail(.liveCall).hasSuffix("Try again when it ends."))
    }

    @Test func theVoiceLLMsRefusalsAreStepsToTakeFirst() {
        #expect(LocalRefusal.of("Choose the voice LLM first: the voice server takes its words from it.")
            == .needsVoiceLLM)
        #expect(LocalRefusal.of("Choose and download the voice LLM's model first.") == .needsVoiceLLM)
        #expect(LocalRefusal.of("The voice LLM's own server isn't serving its model: start it first.")
            == .needsVoiceLLMServer)
        #expect(LocalRefusal.of("The local model server isn't running: start it first.") == .needsServer)
        #expect(LocalRefusal.of("The voice LLM's server isn't answering: check its address.") == nil, "a failure")
    }

    // MARK: - Steps

    @Test func theVoiceLLMsStepsShowWhatItTakesOnlyOnceItCame() {
        #expect(RunSteps.voiceLLM(.here, seen: []).map(\.id)
            == ["download", "runtime", "unit", "start", "check", "configure"])
        #expect(RunSteps.voiceLLM(.here, seen: ["download", "configure", "voice"]).map(\.id)
            == ["download", "runtime", "unit", "start", "check", "configure", "voice"])
        // Restored after a failed start: just after it.
        #expect(RunSteps.voiceLLM(.here, seen: ["runtime", "unit", "start", "restore"]).map(\.id)
            == ["download", "runtime", "unit", "start", "restore", "check", "configure"])
        #expect(RunSteps.voiceLLM(.shared, seen: ["configure", "unit"]).map(\.id) == ["configure", "unit"])
        #expect(RunSteps.voiceLLM(.remote, seen: ["check"]).map(\.id) == ["check", "configure"])
        #expect(RunSteps.voice(seen: ["voice_llm", "install"]).first?.id == "voice_llm")
        #expect(RunSteps.voice(seen: ["install"]).map(\.id) == RunSteps.voice.map(\.id))
    }

    // MARK: - Local Models

    @Test func theMemoryBarShowsTheCatalogsFiguresAndNeverDecidesAFit() {
        // OpenAI in use: only the agent's model.
        let agentOnly = MemoryShares.of(PreviewData.models, voice: PreviewData.voice)
        #expect(agentOnly.shares.map(\.kind) == [.agent])
        #expect(agentOnly.shares.map(\.gigabytes) == [25])
        // The voice here with a model of its own: all three, the speech models' approximate.
        let all = MemoryShares.of(PreviewData.models, voice: PreviewData.voiceOwnLLM)
        #expect(all.shares.map(\.kind) == [.agent, .voiceLLM, .speech])
        #expect(all.shares.map(\.approximate) == [false, false, true])
        #expect(MemoryBar.legend(all.shares[2]) == "Speech models · about \(Formatting.gigabytes(3))")
        #expect(MemoryBar.legend(all.shares[0]) == "Agent: Qwen3-Coder 30B-A3B · \(Formatting.gigabytes(25))")
        // More than the Mac gives models: drawn in proportion across the bar, and nothing
        // said of it. Less: each a part of what it gives.
        #expect(MemoryBar.widths(all, available: 27, total: 340).map(\.width) == [250, 60, 30])
        #expect(MemoryBar.widths(agentOnly, available: 50, total: 100).map(\.width) == [50])
        // Sharing the agent's model: no second share.
        #expect(MemoryShares.of(PreviewData.models, voice: PreviewData.voiceHere).shares.map(\.kind) == [.agent, .speech])
    }

    /// The speech models' share is the CLI's reserve, which it does not print: pinned to the
    /// contract's `constants.speech_gb`, which `tests/test_macos_contract.py` pins to
    /// `catalog.VOICE_SERVER_GB`.
    @Test func theSpeechShareIsTheContractsReserve() throws {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("KeryxKitTests/Fixtures/contract.json")
        let object = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any]
        let constants = try #require(object?["constants"] as? [String: Any])
        let speech = try #require(constants["speech_gb"] as? Double)
        #expect(speech == MemoryShares.speechGigabytes)
    }

    @Test func theVoiceLLMServerSectionSaysWhereTheWordsComeFrom() throws {
        let own = try #require(PreviewData.voiceOwnLLM.llm)
        #expect(VoiceLLMServerWording.isOwn(own, unit: nil))
        let shared = try #require(PreviewData.voice.llm)
        #expect(!VoiceLLMServerWording.isOwn(shared, unit: UnitState(installed: false, active: false)))
        #expect(VoiceLLMServerWording.isOwn(shared, unit: UnitState(installed: true, active: true)),
                "left installed: shown, to be stopped")
        #expect(VoiceLLMServerWording.notInUseTitle(PreviewData.voice) == "The voice shares the local agent's model")
        #expect(VoiceLLMServerWording.modelCaption(own, voice: PreviewData.voiceOwnLLM) == "The voice's words; chosen in Voice")
        #expect(VoiceLLMServerWording.state(own, port: 8091) == "Running at 127.0.0.1:8091")
    }

    @Test func useThisModelDoesWhatItWasAskedFor() {
        #expect(UseModelSheet.plan(.work, model: "m", voice: PreviewData.voiceOwnLLM) == (true, nil))
        #expect(UseModelSheet.plan(.voice, model: "m", voice: PreviewData.voice) == (false, .here(model: "m")))
        #expect(UseModelSheet.plan(.both, model: "m", voice: PreviewData.voiceOwnLLM) == (true, .shared))
        #expect(UseModelSheet.plan(.both, model: "m", voice: PreviewData.voice) == (true, nil), "shared already")
    }

    @Test func useThisModelAsksWhatItIsFor() {
        #expect(UseModelSheet.Purpose.allCases.map { UseModelSheet.title($0, assistant: "Lyra") }
            == ["The Work", "Lyra's Voice", "Both"])
        #expect(UseModelSheet.askingDetail(.voice, assistant: "Lyra").contains("a llama.cpp server of its own"))
        #expect(UseModelSheet.askingDetail(.work, assistant: nil)
            == "Keryx will run it with llama.cpp on this Mac and send tasks to it.")
    }

    // MARK: - Agents, fixes, quitting

    @Test func theLocalAgentSectionPointsAtLocalModels() {
        let footer = ConfigWording.groupFooter("local_agent")
        #expect(footer?.link?.screen == .models)
        #expect(footer?.text.contains("Same as Local Agent") == true)
        #expect(ConfigWording.groupFooter("agents") == nil)
        for key in ["LOCAL_AGENT_BASE_URL", "LOCAL_AGENT_MODEL", "LOCAL_AGENT_API_KEY", "LOCAL_AGENT_API",
                    "VOICE_PIPELINE"] {
            let text = ConfigDescriptions.overrides[key] ?? ""
            #expect(!text.isEmpty && !text.contains("`"), "\(key)")
        }
    }

    @Test func theVoiceLLMsFixStartsItsServerOrAsksForAModelFirst() {
        let fix = FixAction("voice:llm")
        #expect(fix == .voiceLLM)
        var downloaded = PreviewData.models
        downloaded.models = downloaded.models.map { entry in
            var entry = entry
            if entry.name == "qwen3.5-4b" { entry.downloaded = true }
            return entry
        }
        let running = fix?.action(SetupContext(config: PreviewData.config, voice: PreviewData.voiceOwnLLM,
                                               models: downloaded))
        #expect(running == SetupAction(title: "Start Voice LLM Server", effect: .startVoiceLLMServer))
        // Named, not on disk: the server can't start it, `voice llm here` fetches it first.
        let missing = fix?.action(SetupContext(config: PreviewData.config, voice: PreviewData.voiceOwnLLM,
                                               models: PreviewData.models))
        #expect(missing == SetupAction(title: "Download and Start Voice LLM", effect: .setUpVoiceLLM("qwen3.5-4b")))
        var unnamed = PreviewData.voiceOwnLLM
        unnamed.llm?.model = nil
        let choose = fix?.action(SetupContext(config: PreviewData.config, voice: unnamed))
        #expect(choose == SetupAction(title: "Choose the Voice LLM…", effect: .request(.show(.voice))))
        #expect(FixAction("models:choose")?.action(SetupContext()).effect == .request(.show(.models)))
    }

    @Test func settingsUnderOldNamesAreSaidByTheirTitlesAndWhereTheyAre() {
        let titles = ["CLAUDE_MODEL": "Claude model", "DEMO_MODE": "Demo mode"]
        let store = "`keryx config set CLAUDE_MODEL …` saves it under its new name"
        let env = "rename it where Keryx's environment sets it"
        let said = CheckWording.detail("SUBAGENT_MODEL is CLAUDE_MODEL now", titles: titles, hint: store)
        #expect(said.text == "“Claude model” is saved under its old name: changing it saves it under the new one")
        #expect(said.raw == "SUBAGENT_MODEL is CLAUDE_MODEL now")
        let two = CheckWording.detail("SUBAGENT_MODEL is CLAUDE_MODEL now; FAKE_AGENTS is DEMO_MODE now",
                                      titles: titles, hint: store)
        #expect(two.text == "“Claude model” and “Demo mode” are saved under their old names: changing each saves it "
            + "under the new one")
        #expect(!two.text.contains("_"))
        let inEnv = CheckWording.detail("FAKE_AGENTS is DEMO_MODE now", titles: titles, hint: env)
        #expect(inEnv.text == "“Demo mode” is set under its old name in the environment Keryx runs in: rename it there")
        let both = CheckWording.detail("SUBAGENT_MODEL is CLAUDE_MODEL now; FAKE_AGENTS is DEMO_MODE now",
                                       titles: titles, hint: store + "; " + env)
        #expect(both.text.hasSuffix("in Keryx's settings and in the environment Keryx runs in"))
        let voice = CheckWording.detail("the voice server elsewhere is sent OPENAI_VOICE, which a later release stops "
            + "sending to it: set VOICE_SERVER_VOICE instead",
            titles: Wording.titles(PreviewData.config))
        #expect(!voice.text.contains("OPENAI_VOICE") && !voice.text.contains("VOICE_SERVER_VOICE"))
    }

    @Test func quittingNamesAVoiceLLMBeingChangedAndItsServerStarting() {
        let question = Quitting.question(downloading: false, settingUpVoice: false, changingVoiceLLM: true,
                                         assistant: "Lyra")
        #expect(question?.message == "Lyra's voice LLM is being changed. Quit anyway?")
        #expect(question?.detail == "It stops, and the voice is put back on the words it had. Voice changes it again.")
        let server = Quitting.question(downloading: false, settingUpVoice: false, startingVoiceLLMServer: true,
                                       assistant: "Lyra")
        #expect(server?.message == "The voice LLM's server is starting. Quit anyway?")
        #expect(server?.detail == "It stops, and won't start again by itself. Start Voice LLM Server in Local Models "
            + "starts it again.")
    }

    @Test func stoppingTheVoiceLLMsServerAsksFirst() {
        let used = VoiceLLMServerWording.stopQuestion(assistant: "Lyra", usedByVoice: true, liveCalls: 0)
        #expect(used.title == "Stop Lyra's thinking model?")
        #expect(used.message == "Lyra can't answer calls until it starts again.")
        let onACall = VoiceLLMServerWording.stopQuestion(assistant: "Lyra", usedByVoice: true, liveCalls: 1)
        #expect(onACall.message == "Lyra can't answer calls until it starts again. A call is in progress, and Lyra "
            + "stops answering in it.")
        let unused = VoiceLLMServerWording.stopQuestion(assistant: "Lyra", usedByVoice: false, liveCalls: 0)
        #expect(unused.message.contains("doesn't take its words from it"))
    }

    @Test func aChangedSpeechModelSaysWhenItTakesEffect() {
        #expect(VoiceWording.speechSubtitle("How it hears", changed: false, inUse: true) == "How it hears")
        #expect(VoiceWording.speechSubtitle("How it hears", changed: true, inUse: true)
            == "Takes effect when you choose Restart Voice Server")
        #expect(VoiceWording.speechSubtitle("How it hears", changed: true, inUse: false)
            == "Takes effect when you choose Set Up")
    }

    @Test func thePipelineIsSaidInWords() {
        #expect(VoiceWording.pipeline("speech-to-speech") == "one model hears the call and speaks the reply")
        #expect(VoiceWording.pipeline("three-model") == "three models, one to hear, an LLM to think and one to speak")
        #expect(VoiceWording.pipeline("some-new-way") == "some new way")
    }
}
