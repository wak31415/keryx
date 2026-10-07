import AppKit
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
            == "Showing the built-in list — there's no OpenAI API key, so Keryx can't ask which models exist.")
        let naming = Self.list([], live: false, error: "LOCAL_AGENT_BASE_URL isn't set.")
        #expect(OptionWording.note(Loadable(value: naming), titles: Wording.titles(PreviewData.config))
            == "Showing the built-in list — “Local agent server URL” isn't set.")
        #expect(OptionWording.continuing("OpenAI didn't answer.") == "OpenAI didn't answer.")
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
        #expect(VoiceMode.allCases.map(VoiceWording.primary) == ["Use OpenAI Realtime", "Set Up", "Check and Use"])
    }

    @Test func theFooterSaysWhatIsInUseUntilAnotherIsSaved() {
        #expect(VoiceWording.footer(chosen: .openai, inUse: .openai).hasPrefix("In use now."))
        let other = VoiceWording.footer(chosen: .server, inUse: .openai)
        #expect(other.contains("Nothing changes until you choose Check and Use"))
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
    }

    @Test func anAddressReadsAsHostAndPort() {
        #expect(VoiceWording.address("http://studio.local:8000/v1") == "studio.local:8000")
        #expect(VoiceWording.address("https://voice.example.com/v1") == "voice.example.com")
        #expect(VoiceWording.address("not a url") == "not a url")
    }

    @Test func catalogHasEveryVoiceAndOptionsState() {
        let ids = Set(ScreenCatalog.entries().map(\.id))
        for id in ["voice-openai", "voice-openai-no-key", "voice-here", "voice-here-picked", "voice-here-setting-up",
                   "voice-here-setting-up-step", "voice-here-downloading", "voice-here-server-down", "voice-here-chain-server",
                   "voice-here-chain-voice", "voice-here-chain-failed", "voice-here-server-refused", "voice-here-ollama-down",
                   "voice-here-elsewhere-down", "voice-here-server-starting-in-models", "voice-here-stopping", "voice-here-failed", "voice-here-refused", "voice-here-finished",
                   "voice-here-needs-model", "voice-server", "voice-server-picked", "voice-server-stored-key", "voice-server-removing-key", "voice-server-saved-warning",
                   "voice-server-problem", "voice-error", "window-voice",
                   "configuration-model-menus", "configuration-options-unavailable", "configuration-other-sheet",
                   "models-agent-menu-fallback"] {
            #expect(ids.contains(id), "missing \(id)")
        }
    }
}

/// The server key's choices, refusals said by title, quitting, and pop-up menus.
@MainActor @Suite struct VoiceFixRoundTests {
    @Test func theServerKeyIsTypedClearedOrKept() {
        #expect(VoiceWording.serverKey(typed: "vk", clearing: false) == .typed("vk"))
        #expect(VoiceWording.serverKey(typed: "vk", clearing: true) == .typed("vk"), "a typed key wins over removal")
        #expect(VoiceWording.serverKey(typed: "  ", clearing: false) == .stored)
        #expect(VoiceWording.serverKey(typed: nil, clearing: true) == .clear)
        #expect(VoiceWording.serverKey(typed: nil, clearing: false) == .stored)
        #expect(VoiceWording.serverKeyState(typed: false, clearing: false, stored: true) == "Uses the stored key")
        #expect(VoiceWording.serverKeyState(typed: false, clearing: false, stored: false) == "Not set")
        #expect(VoiceWording.serverKeyState(typed: false, clearing: true, stored: true) == "Removed with Check and Use")
        #expect(VoiceWording.serverKeyState(typed: true, clearing: false, stored: true) == "Sent with Check and Use")
    }

    @Test func aRefusalNamesSettingsByTheirTitles() {
        let refusal = KeryxError.failed(command: "keryx voice use", exitCode: 2, message: "VOICE_BASE_URL must start with http")
        #expect(VoiceWording.errorText(refusal, titles: Wording.titles(PreviewData.config))
            == "“Voice server URL” must start with http.")
        #expect(VoiceWording.errorText(.timedOut(command: "keryx voice use"), titles: [:])
            == KeryxError.timedOut(command: "keryx voice use").message)
    }

    @Test func quittingAsksAboutWhatIsStillRunning() {
        #expect(Quitting.question(downloading: false, settingUpVoice: false, assistant: "Lyra") == nil)
        #expect(Quitting.question(downloading: true, settingUpVoice: false, assistant: "Lyra")?.message
            == "A model is downloading. Quit anyway?")
        #expect(Quitting.question(downloading: false, settingUpVoice: true, assistant: "Lyra")?.message
            == "Lyra's voice is being set up. Quit anyway?")
        #expect(Quitting.question(downloading: false, settingUpVoice: true, assistant: nil)?.message
            == "The assistant's voice is being set up. Quit anyway?")
        #expect(Quitting.question(downloading: true, settingUpVoice: true, assistant: "Lyra")?.message
            == "A model is downloading and Lyra's voice is being set up. Quit anyway?")
    }

    @Test func aPopUpButtonsMenuIsNotTheMenuBarMenu() {
        _ = NSApplication.shared
        let window = NSWindow(contentRect: .init(x: 0, y: 0, width: 200, height: 100), styleMask: [],
                              backing: .buffered, defer: true)
        let holder = NSView()
        let popUp = NSPopUpButton(frame: .zero, pullsDown: false)
        popUp.addItems(withTitles: ["Heart", "Emma"])
        holder.addSubview(popUp)
        window.contentView = holder
        #expect(MenuTracking.isPopUpMenu(popUp.menu!, in: [window]))
        #expect(!MenuTracking.isPopUpMenu(NSMenu(title: "Keryx"), in: [window]))
        #expect(!MenuTracking.isPopUpMenu(popUp.menu!, in: []))
    }
}
