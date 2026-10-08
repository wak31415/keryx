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

    @Test func aModelOfItsOwnIsOfferedFromWhatFitsSmallestFirst() {
        let models = PreviewData.models.models
        let choices = VoiceLLMWording.choices(models, current: nil)
        #expect(choices.allSatisfy { $0.fits })
        #expect(choices.map(\.needsGb) == choices.map(\.needsGb).sorted())
        // One in use that no longer fits is still offered, last.
        #expect(VoiceLLMWording.choices(models, current: "gpt-oss-120b").last?.name == "gpt-oss-120b")
        let small = models.first { $0.name == "qwen3.5-4b" }!
        #expect(VoiceLLMWording.menuTitle(small, recommended: "qwen3.5-4b") == "Qwen3.5 4B — Recommended")
        #expect(VoiceLLMWording.hereCaption(nil) == "A small model of its own answers fastest")
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

    @Test func theMemoryBarAddsTheCatalogsFiguresAndNeverDecidesAFit() {
        // OpenAI in use: only the agent's model.
        let agentOnly = MemoryShares.of(PreviewData.models, voice: PreviewData.voice)
        #expect(agentOnly.shares.map(\.kind) == [.agent])
        #expect(agentOnly.total == 25)
        // The voice here with a model of its own: all three.
        let all = MemoryShares.of(PreviewData.models, voice: PreviewData.voiceOwnLLM)
        #expect(all.shares.map(\.kind) == [.agent, .voiceLLM, .speech])
        #expect(all.total == 25 + 6 + MemoryShares.speechGigabytes)
        #expect(MemoryBar.summary(all, available: 27) == "About \(Formatting.gigabytes(34)) of "
            + "\(Formatting.gigabytes(27)): they may not all fit at once")
        #expect(MemoryBar.summary(agentOnly, available: 48).hasSuffix(" in use"))
        // Sharing the agent's model: no second share.
        #expect(MemoryShares.of(PreviewData.models, voice: PreviewData.voiceHere).shares.map(\.kind) == [.agent, .speech])
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
        let running = fix?.action(SetupContext(config: PreviewData.config, voice: PreviewData.voiceOwnLLM))
        #expect(running == SetupAction(title: "Start Voice LLM Server", effect: .startVoiceLLMServer))
        var unnamed = PreviewData.voiceOwnLLM
        unnamed.llm?.model = nil
        let choose = fix?.action(SetupContext(config: PreviewData.config, voice: unnamed))
        #expect(choose == SetupAction(title: "Choose the Voice LLM…", effect: .request(.show(.voice))))
        #expect(FixAction("models:choose")?.action(SetupContext()).effect == .request(.show(.models)))
    }

    @Test func aSettingUnderAnOldNameIsSaidByItsTitle() {
        let said = CheckWording.detail("SUBAGENT_MODEL is CLAUDE_MODEL now", titles: ["CLAUDE_MODEL": "Claude model"])
        #expect(said.text == "“Claude model” is saved under an old name: changing it saves it under the new one")
        #expect(said.raw == "SUBAGENT_MODEL is CLAUDE_MODEL now")
        let voice = CheckWording.detail("the voice server elsewhere is sent OPENAI_VOICE, which a later release stops "
            + "sending to it: set VOICE_SERVER_VOICE instead",
            titles: Wording.titles(PreviewData.config))
        #expect(!voice.text.contains("OPENAI_VOICE") && !voice.text.contains("VOICE_SERVER_VOICE"))
    }

    @Test func quittingNamesAVoiceLLMBeingChanged() {
        let question = Quitting.question(downloading: false, settingUpVoice: false, changingVoiceLLM: true,
                                         assistant: "Lyra")
        #expect(question?.message == "Lyra's voice LLM is being changed. Quit anyway?")
        #expect(question?.detail == "It stops, and the voice keeps the words it had. Voice changes it again.")
    }

    @Test func thePipelineIsSaidInWords() {
        #expect(VoiceWording.pipeline("speech-to-speech") == "one model hears the call and speaks the reply")
        #expect(VoiceWording.pipeline("three-model") == "three models, one to hear, an LLM to think and one to speak")
        #expect(VoiceWording.pipeline("some-new-way") == "some new way")
    }
}
