import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// The option menus (Configuration, Voice, Local Models) and the Voice screen's wording.
@MainActor @Suite struct VoiceScreenTests {
    // MARK: - Option menus

    static func list(_ options: [ConfigOption], live: Bool = true, freeForm: Bool = true,
                     error: String? = nil) -> ConfigOptions {
        ConfigOptions(key: "K", current: nil, options: options, source: live ? "openai" : "builtin", live: live,
                      freeForm: freeForm, error: error)
    }

    @Test func theMenuShowsLabelsAndTheValueChosenEvenWhenTheListLacksIt() {
        let options = [ConfigOption(value: "a", label: "Alpha"), ConfigOption(value: "b", label: "Beta")]
        #expect(OptionWording.items(Self.list(options), current: "a").map(\.label) == ["Alpha", "Beta"])
        #expect(OptionWording.items(Self.list(options), current: "z").map(\.value) == ["z", "a", "b"])
        #expect(OptionWording.items(Self.list(options), current: "").first?.label == "Not set")
        // Before the list is read: the value chosen alone.
        #expect(OptionWording.items(nil, current: "a") == [.init(value: "a", label: "a")])
    }

    @Test func anEmptyOptionIsItsOwnItemNotDefault() {
        let codex = PreviewData.options["CODEX_MODEL"]
        let items = OptionWording.items(codex, current: "")
        #expect(items.first == .init(value: "", label: "Codex's default"))
        #expect(items.filter { $0.value.isEmpty }.count == 1)
    }

    @Test func otherIsOfferedWhenTheListAllowsItOrCouldNotBeRead() {
        #expect(OptionWording.offersOther(Loadable(value: Self.list([], freeForm: true))))
        #expect(!OptionWording.offersOther(Loadable(value: Self.list([], freeForm: false))))
        #expect(OptionWording.offersOther(Loadable(error: .timedOut(command: "keryx config options"))))
        #expect(!OptionWording.offersOther(nil))
        #expect(!OptionWording.offersOther(.idle))
        #expect(!OptionWording.other.isEmpty && !PreviewData.options.values.contains {
            $0.options.contains { $0.value == OptionWording.other }
        })
    }

    @Test func aFallbackListSaysSoAndALiveOneSaysNothing() {
        #expect(OptionWording.note(Loadable(value: Self.list([]))) == nil)
        #expect(OptionWording.note(Loadable(value: Self.list([], live: false))) == nil, "a built-in list by design")
        #expect(OptionWording.note(Loadable(value: Self.list([], live: false, error: "OpenAI didn't answer")))
            == "Showing the built-in list — OpenAI didn't answer")
        let failure = KeryxError.failed(command: "keryx config options", exitCode: 2, message: "No such command 'options'.")
        #expect(OptionWording.note(Loadable(error: failure)) == "Couldn't list the choices — No such command 'options'.")
        #expect(OptionWording.note(nil) == nil)
        #expect(OptionWording.note(Loadable(value: PreviewData.realtimeFallback), titles: Wording.titles(PreviewData.config))
            == "Showing the built-in list — “OpenAI API key” is not set, so OpenAI cannot be asked")
    }

    @Test func aDetailIsShownWhenItSaysSomethingTheLabelDoesNot() {
        let kokoro = PreviewData.options["VOICE_SERVER_VOICE"]
        #expect(OptionWording.detail(of: "af_heart", in: kokoro) == "Warm, American, female")
        let claude = PreviewData.options["SUBAGENT_MODEL"]
        #expect(OptionWording.detail(of: "claude-opus-5-5", in: claude) == nil, "“opus” under “Claude Opus”")
        let current = Self.list([ConfigOption(value: "x", label: "x", detail: "current")])
        #expect(OptionWording.detail(of: "x", in: current) == nil)
        #expect(OptionWording.detail(of: "missing", in: kokoro) == nil)
    }

    @Test func onlyAPlainSettingWithOptionsIsAMenu() {
        let rows = PreviewData.config
        let menus = rows.filter(ConfigWording.hasMenu).map(\.key)
        #expect(Set(menus) == Set(rows.filter(\.hasOptions).map(\.key)))
        #expect(!menus.contains("OPENAI_API_KEY"))
        var secret = rows.first { $0.key == "OPENAI_API_KEY" }!
        secret.hasOptions = true
        #expect(!ConfigWording.hasMenu(secret))
    }

    // MARK: - Voice

    @Test func theModesHaveTheirNames() {
        #expect(VoiceMode.allCases.map(VoiceWording.title) == ["OpenAI Realtime", "This Mac", "A Server Elsewhere"])
        #expect(VoiceWording.sectionTitle("Lyra") == "How Lyra Speaks")
        #expect(VoiceWording.sectionTitle(nil) == "How the Assistant Speaks")
        #expect(VoiceMode.allCases.map(VoiceWording.primary) == ["Use OpenAI Realtime", "Set Up", "Check & Use"])
    }

    @Test func theFooterSaysWhatIsInUseUntilAnotherIsSaved() {
        #expect(VoiceWording.footer(chosen: .openai, inUse: .openai).hasPrefix("In use now."))
        let other = VoiceWording.footer(chosen: .server, inUse: .openai)
        #expect(other.contains("Nothing changes until you choose Check & Use"))
        #expect(other.contains("OpenAI Realtime stays in use"))
    }

    @Test func thisMacSaysWhatItsServerIsDoing() {
        var here = PreviewData.voiceHere.here
        #expect(VoiceWording.hereState(here) == "Running at 127.0.0.1:8765")
        here.reachable = false
        #expect(VoiceWording.hereState(here) == "Installed, not running")
        here.installed = false
        #expect(VoiceWording.hereState(here) == "Not installed")
        #expect(VoiceWording.wordsModel(nil, models: nil) == "None yet")
        #expect(VoiceWording.wordsModel("qwen3-coder-30b-a3b", models: PreviewData.models.models) == "Qwen3-Coder 30B-A3B")
        #expect(VoiceWording.wordsModel("unknown", models: PreviewData.models.models) == "unknown")
    }

    @Test func aStepReadsAsARunningSentence() {
        #expect(VoiceWording.running("downloading the speech models") == "Downloading the speech models…")
        #expect(VoiceWording.running("Starting the voice server.") == "Starting the voice server…")
        #expect(VoiceWording.running("") == VoiceSetup.starting)
        #expect(VoiceWording.stepCount("unit") == "Step 3 of 4")
        #expect(VoiceWording.stepCount("something new") == nil)
        let sizes = VoiceSetupEvent.Progress(done: 120_000_000, total: 480_000_000)
        #expect(VoiceWording.sizes(sizes) == ModelRow.progress(done: 120_000_000, total: 480_000_000))
    }

    @Test func anAddressReadsAsHostAndPort() {
        #expect(VoiceWording.address("http://studio.local:8000/v1") == "studio.local:8000")
        #expect(VoiceWording.address("https://voice.example.com/v1") == "voice.example.com")
        #expect(VoiceWording.address("not a url") == "not a url")
    }

    @Test func catalogHasEveryVoiceAndOptionsState() {
        let ids = Set(ScreenCatalog.entries().map(\.id))
        for id in ["voice-openai", "voice-openai-no-key", "voice-here", "voice-here-picked", "voice-here-setting-up",
                   "voice-here-setting-up-step", "voice-here-stopping", "voice-here-failed", "voice-here-finished",
                   "voice-here-needs-model", "voice-server", "voice-server-picked", "voice-server-saved-warning",
                   "voice-server-problem", "voice-error", "window-voice",
                   "configuration-model-menus", "configuration-options-unavailable", "configuration-other-sheet",
                   "models-agent-menu-fallback"] {
            #expect(ids.contains(id), "missing \(id)")
        }
    }
}
