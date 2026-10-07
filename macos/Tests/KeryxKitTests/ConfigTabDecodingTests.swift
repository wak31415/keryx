import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview

/// `config list --json`'s tab placement (`tab`, `tab_title`, `tab_order`, `advanced`), and
/// what an older CLI that sends none of it falls back to.
@Suite struct ConfigTabDecodingTests {
    private func decode(_ extra: String, group: String = "local") throws -> ConfigRow {
        let json = """
        {"key": "LLM_SERVER_PORT", "group": "\(group)", \(extra) "description": "The port.",
         "source": "default", "type": "int", "required": false, "set": false, "secret": false,
         "service_writable": true, "protected": false, "value": 8090, "choices": null}
        """
        return try JSON.decoder.decode(ConfigRow.self, from: Data(json.utf8))
    }

    @Test func aRowSaysWhichTabItIsOnAndWhetherItIsAdvanced() throws {
        let row = try decode(#""tab": "local", "tab_title": "Local Models", "tab_order": 3, "advanced": true,"#)
        #expect(row.tab == "local")
        #expect(row.tabTitle == "Local Models")
        #expect(row.tabOrder == 3)
        #expect(row.advanced)
    }

    @Test func anOlderCLIsRowIsPlacedByItsGroupAndIsAKeySetting() throws {
        let row = try decode("")
        #expect(row.tab == "local")
        #expect(row.tabTitle == "Local Models")
        #expect(row.tabOrder == 3)
        #expect(!row.advanced)
        #expect(try decode("", group: "owner").tab == "general")
        #expect(try decode("", group: "google").tab == "integrations")
        #expect(try decode("", group: "plugins").tab == "integrations")
        #expect(try decode("", group: "projects").tab == "integrations")
        #expect(try decode("", group: "limits").tab == "system")
        #expect(try decode("", group: "service").tab == "system")
        #expect(try decode("", group: "debug").tabTitle == "System")
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
        #expect(row.tabOrder == 4)
    }

    @Test func theFixtureIsOnTheEightTabsWithAKeySettingOnEach() {
        let rows = PreviewData.config
        let tabs = Set(rows.map(\.tab))
        #expect(tabs == Set(ConfigRow.knownTabs))
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
    }
}
