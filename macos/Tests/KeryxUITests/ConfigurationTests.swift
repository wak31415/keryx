import Foundation
import Testing
@testable import KeryxKit
@testable import KeryxUI

@MainActor @Suite struct ConfigurationTests {
    func row(_ key: String, group: String = "voice", type: String = "string", source: String = "default",
             value: JSONValue? = nil, description: String = "", secret: Bool = false) -> ConfigRow {
        ConfigRow(key: key, group: group, description: description, source: source, type: type,
                  required: false, set: source != "default", secret: secret, serviceWritable: false,
                  protected: false, value: value, choices: nil)
    }

    @Test func groupsHaveTheirTitles() {
        let titles = ["voice": "Voice", "agents": "Coding Agents", "local": "Local Models", "phone": "Phone",
                      "owner": "You", "projects": "Projects", "limits": "Limits", "approvals": "Approvals",
                      "plugins": "Plugins", "google": "Google", "service": "Service", "debug": "Debugging"]
        for (group, title) in titles {
            #expect(ConfigWording.title(ofGroup: group) == title)
        }
        #expect(ConfigWording.title(ofGroup: "storage") == "Storage")
    }

    @Test func groupsKeepTheOrderConfigListGives() {
        let rows = [row("A", group: "phone"), row("B", group: "voice"), row("C", group: "phone")]
        let groups = ConfigWording.groups(rows)
        #expect(groups.map(\.group) == ["phone", "voice"])
        #expect(groups[0].rows.map(\.key) == ["A", "C"])
    }

    @Test func searchMatchesKeyAndDescriptionIgnoringCase() {
        let rows = [row("VAD_MODE", description: "How the assistant decides"),
                    row("SMS_ENABLED", description: "Keryx does not text unless this is on"),
                    row("DATA_DIR", description: "Where the memory lives")]
        #expect(ConfigWording.filter(rows, by: "vad").map(\.key) == ["VAD_MODE"])
        #expect(ConfigWording.filter(rows, by: "TEXT").map(\.key) == ["SMS_ENABLED"])
        #expect(ConfigWording.filter(rows, by: "  ").count == 3)
        #expect(ConfigWording.filter(rows, by: "nothing like it").isEmpty)
    }

    @Test func sourcesReadAsWhereTheValueCameFrom() {
        #expect(ConfigWording.source("default") == nil)
        #expect(ConfigWording.source("config.toml") == "from config.toml")
        #expect(ConfigWording.source("secrets.toml") == "from secrets.toml")
        #expect(ConfigWording.source("environment") == "from the environment")
        #expect(ConfigWording.source("command line") == "from the command line")
        #expect(ConfigWording.source("KERYX_HOME/pin") == "from KERYX_HOME/pin")
    }

    @Test func onlyTheEnvironmentIsReadOnly() {
        #expect(ConfigWording.readOnly(row("PUBLIC_HOST", source: "environment")))
        #expect(!ConfigWording.readOnly(row("OWNER_NUMBER", source: "config.toml")))
        var protected = row("APPROVAL_MAX_PER_HOUR")
        protected.protected = true
        #expect(!ConfigWording.readOnly(protected), "protected binds only the service")
    }

    @Test func valuesEditAsTheCLIReadsThem() {
        #expect(ConfigWording.editText(.array([.string("claude"), .string("local")])) == "claude, local")
        #expect(ConfigWording.editText(.number(300)) == "300")
        #expect(ConfigWording.editText(.number(0.5)) == "0.5")
        #expect(ConfigWording.editText(nil) == "")
        #expect(ConfigWording.editText(.null) == "")
    }

    @Test func descriptionsDropTheirBackticksForTooltips() {
        #expect(ConfigWording.plain("Unless `VOICE_BASE_URL` names one") == "Unless VOICE_BASE_URL names one")
    }

    @Test func theScreenIsInTheCatalogInEveryState() {
        let ids = Set(ScreenCatalog.entries().map(\.id))
        for id in ["configuration", "configuration-search", "configuration-write-error",
                   "configuration-needs-restart", "configuration-secret-sheet", "configuration-lower-groups",
                   "window-configuration"] {
            #expect(ids.contains(id), "missing \(id)")
        }
    }
}
