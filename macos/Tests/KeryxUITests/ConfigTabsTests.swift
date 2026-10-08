import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// Configuration's tabs: in `tab_order`, each split into its key settings by group and its
/// advanced ones, and a search across all of them grouped by tab.
@MainActor @Suite struct ConfigTabsTests {
    func row(_ key: String, group: String = "voice", tab: String? = nil, title: String? = nil, order: Int? = nil,
             advanced: Bool = false, groupTitle: String? = nil, description: String = "") -> ConfigRow {
        ConfigRow(key: key, group: group, title: nil, groupTitle: groupTitle, tab: tab, tabTitle: title, tabOrder: order,
                  advanced: advanced, description: description, source: "default", type: "string",
                  required: false, set: false, secret: false, serviceWritable: true, protected: false,
                  value: nil, choices: nil)
    }

    @Test func tabsComeInTabOrderNotTheOrderTheRowsDo() {
        let rows = [row("A", tab: "voice", title: "Voice", order: 1),
                    row("B", group: "owner", tab: "general", title: "General", order: 0),
                    row("C", tab: "voice", title: "Voice", order: 1)]
        let tabs = ConfigWording.tabs(rows)
        #expect(tabs.map(\.id) == ["general", "voice"])
        #expect(tabs[1].rows.map(\.key) == ["A", "C"])
    }

    @Test func tabsWithTheSameOrderKeepTheOrderConfigListNamesThem() {
        let rows = [row("A", tab: "b", title: "B", order: 5), row("B", tab: "a", title: "A", order: 5)]
        #expect(ConfigWording.tabs(rows).map(\.id) == ["b", "a"])
    }

    @Test func theFixtureMakesTheSevenTabsInOrder() {
        let tabs = ConfigWording.tabs(PreviewData.config)
        #expect(tabs.map(\.id) == ["general", "voice", "agents", "phone", "approvals", "integrations", "system"])
        #expect(tabs.map(\.title) == ["General", "Voice", "Agents", "Phone", "Approvals", "Integrations", "System"])
        #expect(tabs.map(\.symbol) == ["gearshape", "waveform", "cpu", "phone",
                                       "checkmark.shield", "puzzlepiece.extension", "wrench.and.screwdriver"])
        #expect(tabs.allSatisfy { !$0.keyRows.isEmpty })
        let onScreens = PreviewData.config.filter { $0.screen != nil }
        #expect(tabs.reduce(0) { $0 + $1.rows.count } + onScreens.count == PreviewData.config.count,
                "every row on one tab, or on a screen")
        #expect(ConfigWording.symbol(ofTab: "storage") == "ellipsis.circle")
        #expect(ConfigWording.symbol(ofTab: "local") == "ellipsis.circle", "the Local Models tab is gone")
    }

    @Test func aRowAScreenOwnsIsOnNoTab() {
        let tabs = ConfigWording.tabs(PreviewData.config)
        let keys = Set(tabs.flatMap(\.rows).map(\.key))
        for key in ["OPENAI_REALTIME_MODEL", "OPENAI_VOICE", "VOICE_MODE", "VOICE_BASE_URL", "VOICE_SERVER_STT",
                    "VOICE_LLM", "VOICE_SERVER_ARGS", "AGENT_SERVER_MODEL", "AGENT_SERVER_PORT", "VOICE_SERVER_PORT"] {
            #expect(!keys.contains(key), "\(key) has its editor on a screen")
        }
        // Its group's neighbours that no screen owns stay: the Realtime tuning, the local agent.
        let voice = tabs.first { $0.id == "voice" }
        #expect(voice?.advanced.map(\.group).contains("realtime") == true)
        #expect(tabs.first { $0.id == "agents" }?.rows.contains { $0.key == "LOCAL_AGENT_MODEL" } == true)
    }

    @Test func aTabSplitsItsKeySettingsFromItsAdvancedOnesByGroup() {
        let rows = [row("LIMIT", group: "limits", tab: "system", order: 7, advanced: true),
                    row("CAP", group: "limits", tab: "system", order: 7),
                    row("DATA", group: "service", tab: "system", order: 7, advanced: true),
                    row("DEMO", group: "debug", tab: "system", order: 7, advanced: true)]
        let tab = ConfigWording.tabs(rows)[0]
        #expect(tab.keyRows.map(\.key) == ["CAP"])
        #expect(tab.groups.map(\.group) == ["limits"])
        #expect(tab.advanced.map(\.group) == ["limits", "service", "debug"])
        #expect(tab.advancedRows.map(\.key) == ["LIMIT", "DATA", "DEMO"])
        #expect(ConfigWording.advancedCount(tab) == "3 settings")
        #expect(ConfigWording.advancedSectionTitle(tab.advanced[1], on: tab) == "Service",
                "several groups on the tab: each advanced one is named")
    }

    @Test func aTabOfOneGroupDoesNotRepeatItsName() {
        let rows = [row("A", tab: "voice", title: "Voice", order: 1, groupTitle: "Voice"),
                    row("B", tab: "voice", title: "Voice", order: 1, advanced: true, groupTitle: "Voice")]
        let tab = ConfigWording.tabs(rows)[0]
        #expect(ConfigWording.sectionTitle(tab.groups[0], on: tab) == nil)
        #expect(ConfigWording.advancedSectionTitle(tab.advanced[0], on: tab) == nil)
        #expect(ConfigWording.advancedCount(tab) == "1 setting")
        let agents = ConfigWording.tabs([row("C", group: "agents", tab: "agents", title: "Agents", order: 2,
                                             groupTitle: "Coding agents")])[0]
        #expect(ConfigWording.sectionTitle(agents.groups[0], on: agents) == "Coding Agents")
    }

    @Test func anOlderCLIsRowsFallIntoTheTabsByGroupAsKeySettings() {
        let rows = [row("OWNER_NAME", group: "owner"), row("DEMO_MODE", group: "debug"),
                    row("SLACK_BOT_TOKEN", group: "plugins"), row("VAD_MODE", group: "voice")]
        let tabs = ConfigWording.tabs(rows)
        #expect(tabs.map(\.id) == ["general", "voice", "integrations", "system"])
        #expect(tabs.allSatisfy { $0.advanced.isEmpty })
    }

    @Test func theTabShownIsTheOneChosenWhileItIsThere() {
        let tabs = ConfigWording.tabs(PreviewData.config)
        #expect(ConfigWording.selected(nil, in: tabs)?.id == "general")
        #expect(ConfigWording.selected("phone", in: tabs)?.id == "phone")
        #expect(ConfigWording.selected("gone", in: tabs)?.id == "general")
        #expect(ConfigWording.selected("phone", in: []) == nil)
    }

    @Test func aSearchLooksThroughEveryTabAdvancedSettingsIncluded() {
        let found = ConfigWording.results(PreviewData.config, for: "api key")
        #expect(found.map(\.id) == ["voice", "agents"])
        let agents = found.first { $0.id == "agents" }
        #expect(agents?.advancedRows.map(\.key) == ["LOCAL_AGENT_API_KEY"])
        #expect(ConfigWording.resultTitle(found[0], advanced: false) == "Voice")
        #expect(ConfigWording.resultTitle(agents!, advanced: true) == "Agents › Advanced")
        #expect(ConfigWording.results(PreviewData.config, for: "nothing like it").isEmpty)
        #expect(ConfigWording.results(PreviewData.config, for: " ").count == 7, "a blank search is every tab")
    }

    @Test func aSearchFindsARowAScreenOwnsAsTheWayThere() {
        let found = ConfigWording.screenResults(PreviewData.config, for: "api key")
        #expect(found.map(\.screen) == [.voice])
        #expect(found[0].rows.map(\.key) == ["VOICE_API_KEY", "VOICE_LLM_API_KEY"])
        #expect(found[0].title == "Voice")
        #expect(found[0].link == "Change in Voice ›")
        let ports = ConfigWording.screenResults(PreviewData.config, for: "server port")
        #expect(ports.map(\.screen) == [.models])
        #expect(ports[0].link == "Change in Local Models ›")
        #expect(ports[0].rows.map(\.key) == ["AGENT_SERVER_PORT", "VOICE_SERVER_PORT", "VOICE_LLM_SERVER_PORT"])
        #expect(ConfigWording.screenResults(PreviewData.config, for: "nothing like it").isEmpty)
        // A screen this build has no section for has nowhere to send its rows.
        let unknown = ConfigRow(key: "X_PORT", group: "x", screen: "elsewhere", description: "", source: "default",
                                type: "int", required: false, set: false, secret: false, serviceWritable: false,
                                protected: false, value: nil, choices: nil)
        #expect(ConfigWording.screenResults([unknown], for: "port").isEmpty)
    }

    @Test func aSettingAScreenOwnsIsAskedForOnThatScreen() {
        let rows = PreviewData.config
        #expect(ConfigWording.screen(of: "VOICE_SERVER_ARGS", in: rows) == .voice)
        #expect(ConfigWording.screen(of: "AGENT_SERVER_MODEL", in: rows) == .models)
        #expect(ConfigWording.screen(of: "ALLOWED_CALLERS", in: rows) == nil)
        #expect(ConfigWording.screen(of: "VOICE_SERVER_ARGS", in: nil) == nil, "before the settings are read")
        // Asked of Configuration anyway, it shows the first tab and focuses nothing.
        let first = rows.lazy.compactMap(\.tab).first!
        #expect(ConfigWording.answer(.setting(key: "VOICE_SERVER_ARGS"), pinTab: nil, config: rows)! == (first, nil, nil))
    }

    @Test func eachScreenShowsItsOwnAdvancedSettings() {
        let rows = PreviewData.config
        #expect(ScreenAdvancedSection.rows(rows, on: .voice).map(\.key) == ["VOICE_SERVER_ARGS"])
        #expect(ScreenAdvancedSection.rows(rows, on: .models).map(\.key)
            == ["AGENT_SERVER_PORT", "VOICE_SERVER_PORT", "VOICE_LLM_SERVER_PORT"])
        #expect(ScreenAdvancedSection.rows(rows, on: .phone).isEmpty)
    }

    @Test func thePINIsAKeySettingOnGeneral() {
        let general = ConfigWording.tabs(PreviewData.config)[0]
        #expect(general.keyRows.contains { ConfigWording.isPIN($0) })
    }
}
