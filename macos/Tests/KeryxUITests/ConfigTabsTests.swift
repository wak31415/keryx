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

    @Test func theFixtureMakesTheEightTabsInOrder() {
        let tabs = ConfigWording.tabs(PreviewData.config)
        #expect(tabs.map(\.id) == ["general", "voice", "agents", "local", "phone", "approvals", "integrations", "system"])
        #expect(tabs.map(\.title) == ["General", "Voice", "Agents", "Local Models", "Phone", "Approvals",
                                      "Integrations", "System"])
        #expect(tabs.map(\.symbol) == ["gearshape", "waveform", "cpu", "desktopcomputer", "phone",
                                       "checkmark.shield", "puzzlepiece.extension", "wrench.and.screwdriver"])
        #expect(tabs.allSatisfy { !$0.keyRows.isEmpty })
        #expect(tabs.reduce(0) { $0 + $1.rows.count } == PreviewData.config.count, "every row on one tab")
        #expect(ConfigWording.symbol(ofTab: "storage") == "ellipsis.circle")
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
        #expect(found.map(\.id) == ["voice", "agents", "local"])
        let local = found.first { $0.id == "local" }
        #expect(local?.keyRows.isEmpty == true)
        #expect(local?.advancedRows.map(\.key) == ["VOICE_API_KEY", "LOCAL_AGENT_API_KEY"])
        #expect(ConfigWording.resultTitle(found[0], advanced: false) == "Voice")
        #expect(ConfigWording.resultTitle(local!, advanced: true) == "Local Models › Advanced")
        #expect(ConfigWording.results(PreviewData.config, for: "nothing like it").isEmpty)
        #expect(ConfigWording.results(PreviewData.config, for: " ").count == 8, "a blank search is every tab")
    }

    @Test func thePINIsAKeySettingOnGeneral() {
        let general = ConfigWording.tabs(PreviewData.config)[0]
        #expect(general.keyRows.contains { ConfigWording.isPIN($0) })
    }
}
