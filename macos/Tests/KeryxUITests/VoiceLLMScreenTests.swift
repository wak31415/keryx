import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
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

    @Test func aModelOfItsOwnIsMarkedByTheCLIsFitNeverTheAgents() {
        let models = PreviewData.models.models
        let choices = VoiceLLMWording.choices(models, current: nil)
        // The catalog, smallest first: whether each fits is said by `fits_with_voice_llm`.
        #expect(choices.count == models.count)
        #expect(choices.map(\.needsGb) == choices.map(\.needsGb).sorted())
        // qwen3.8-27b fits as the agent's (`fits`), but not beside a voice of its own.
        let big = models.first { $0.name == "qwen3.8-27b" }!
        #expect(big.fits && big.fitsWithVoiceLlm == false)
        #expect(VoiceLLMWording.menuTitle(big, recommended: nil) == "Qwen3.8 27B")
        #expect(VoiceLLMWording.hereCaption(big).hasSuffix(", more than is free"))
        let small = models.first { $0.name == "qwen3.5-4b" }!
        #expect(VoiceLLMWording.menuTitle(small, recommended: "qwen3.5-4b") == "Qwen3.5 4B — Recommended, Fits")
        #expect(VoiceLLMWording.hereCaption(small).hasSuffix(", and fits"))
        // An older CLI says nothing of it: no mark at all, rather than a wrong one.
        var older = small
        older.fitsWithVoiceLlm = nil
        #expect(VoiceLLMWording.menuTitle(older, recommended: nil) == "Qwen3.5 4B")
        #expect(!VoiceLLMWording.hereCaption(older).contains("fit"))
        #expect(VoiceLLMWording.hereCaption(nil) == "A small model of its own answers fastest")
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

    @Test func theServersStateAndAddressesAreSaidPlainly() {
        let own = PreviewData.voiceOwnLLM.llm!
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

    /// The speech models' share is the CLI's reserve, which it does not print: pinned to it.
    @Test func theSpeechShareIsTheCLIsReserve() throws {
        let catalog = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("src/keryx/localmodels/catalog.py")
        let text = try String(contentsOf: catalog, encoding: .utf8)
        let match = try #require(text.firstMatch(of: #/\nVOICE_SERVER_GB(?:: *[A-Za-z]+)? *= *([0-9.]+)/#))
        #expect(Double(String(match.1)) == MemoryShares.speechGigabytes)
    }

    @Test func theVoiceLLMServerSectionSaysWhereTheWordsComeFrom() {
        let own = PreviewData.voiceOwnLLM.llm!
        #expect(VoiceLLMServerWording.isOwn(own, unit: nil))
        let shared = PreviewData.voice.llm!
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
        #expect(server?.detail == "It stops, and its service is removed. Start Voice LLM Server in Local Models "
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
