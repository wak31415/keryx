import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview

/// `config list --json`'s tab placement (`tab`, `tab_title`, `tab_order`, `advanced`) and
/// the screen that owns a row instead (`screen`), and what an older CLI that sends none of
/// it falls back to.
@Suite struct ConfigTabDecodingTests {
    private func decode(_ extra: String, group: String = "local_agent") throws -> ConfigRow {
        let json = """
        {"key": "AGENT_SERVER_PORT", "group": "\(group)", \(extra) "description": "The port.",
         "source": "default", "type": "int", "required": false, "set": false, "secret": false,
         "service_writable": true, "protected": false, "value": 8090, "choices": null}
        """
        return try JSON.decoder.decode(ConfigRow.self, from: Data(json.utf8))
    }

    @Test func aRowSaysWhichTabItIsOnAndWhetherItIsAdvanced() throws {
        let row = try decode(#""tab": "agents", "tab_title": "Agents", "tab_order": 2, "screen": null, "advanced": true,"#)
        #expect(row.tab == "agents")
        #expect(row.tabTitle == "Agents")
        #expect(row.tabOrder == 2)
        #expect(row.screen == nil)
        #expect(row.advanced)
    }

    @Test func aRowAScreenOwnsIsOnNoTab() throws {
        let row = try decode(#""tab": null, "tab_title": null, "tab_order": null, "screen": "models", "advanced": true,"#,
                             group: "model_servers")
        #expect(row.screen == "models")
        #expect(row.tab == nil && row.tabTitle == nil && row.tabOrder == nil)
        #expect(row.advanced)
    }

    @Test func aNullScreenFromTheCLIIsBelievedOverTheGroup() throws {
        // The CLI says where a group is; this build's table is only for a CLI that does not.
        let row = try decode(#""tab": "voice", "tab_title": "Voice", "tab_order": 1, "screen": null,"#,
                             group: "voice_model")
        #expect(row.screen == nil)
        #expect(row.tab == "voice")
    }

    @Test func anOlderCLIsRowIsPlacedByItsGroupAndIsAKeySetting() throws {
        let row = try decode("")
        #expect(row.tab == "agents")
        #expect(row.tabTitle == "Agents")
        #expect(row.tabOrder == 2)
        #expect(row.screen == nil)
        #expect(!row.advanced)
        #expect(try decode("", group: "owner").tab == "general")
        #expect(try decode("", group: "realtime").tab == "voice")
        #expect(try decode("", group: "google").tab == "integrations")
        #expect(try decode("", group: "plugins").tab == "integrations")
        #expect(try decode("", group: "projects").tab == "integrations")
        #expect(try decode("", group: "limits").tab == "system")
        #expect(try decode("", group: "service").tab == "system")
        #expect(try decode("", group: "debug").tabTitle == "System")
        let voice = try decode("", group: "voice_model")
        #expect(voice.screen == "voice" && voice.tab == nil && voice.tabOrder == nil)
        #expect(try decode("", group: "model_servers").screen == "models")
    }

    @Test func aGroupThisBuildDoesNotKnowGetsATabOfItsOwnAfterTheRest() throws {
        let row = try decode("", group: "storage")
        #expect(row.tab == "storage")
        #expect(row.tabTitle == "Storage")
        #expect(row.tabOrder == ConfigRow.knownTabs.count)
    }

    @Test func aTabTheCLINamesWithoutATitleOrOrderIsStillPlaced() throws {
        let row = try decode(#""tab": "phone","#, group: "voice")
        #expect(row.tab == "phone")
        #expect(row.tabTitle == "Phone")
        #expect(row.tabOrder == 3)
    }

    @Test func theFixtureIsOnTheSevenTabsWithAKeySettingOnEach() {
        let rows = PreviewData.config
        let tabs = Set(rows.compactMap(\.tab))
        #expect(tabs == Set(ConfigRow.knownTabs))
        #expect(!ConfigRow.knownTabs.contains("local"))
        // A row is on a tab or a screen, never both and never neither.
        #expect(rows.allSatisfy { ($0.tab == nil) != ($0.screen == nil) })
        #expect(Set(rows.compactMap(\.screen)) == ["voice", "models"])
        #expect(rows.filter { $0.screen != nil }.allSatisfy { $0.tabTitle == nil && $0.tabOrder == nil })
        for tab in tabs {
            #expect(rows.contains { $0.tab == tab && !$0.advanced }, "\(tab) has no key setting")
        }
        #expect(rows.filter { $0.group == "debug" }.allSatisfy { $0.advanced })
        // Each tab's order and title is the same on every one of its rows.
        for tab in tabs {
            let placed = rows.filter { $0.tab == tab }
            #expect(Set(placed.map(\.tabOrder)).count == 1)
            #expect(Set(placed.map(\.tabTitle)).count == 1)
        }
    }

    @Test func theInitialiserPlacesARowByItsGroupUnlessTold() {
        let row = ConfigRow(key: "OWNER_NAME", group: "owner", description: "", source: "default", type: "string",
                            required: false, set: false, secret: false, serviceWritable: true,
                            protected: false, value: nil, choices: nil)
        #expect(row.tab == "general")
        #expect(row.tabOrder == 0)
        #expect(!row.advanced)
        let told = ConfigRow(key: "X", group: "owner", tab: "system", tabTitle: "Machine", tabOrder: 9, advanced: true,
                             description: "", source: "default", type: "string", required: false, set: false,
                             secret: false, serviceWritable: true, protected: false, value: nil, choices: nil)
        #expect(told.tab == "system")
        #expect(told.tabTitle == "Machine")
        #expect(told.tabOrder == 9)
        #expect(told.advanced)
        let owned = ConfigRow(key: "VOICE_SERVER_PORT", group: "model_servers", description: "", source: "default",
                              type: "int", required: false, set: false, secret: false, serviceWritable: false,
                              protected: false, value: nil, choices: nil)
        #expect(owned.screen == "models")
        #expect(owned.tab == nil && owned.tabTitle == nil && owned.tabOrder == nil)
    }
}
