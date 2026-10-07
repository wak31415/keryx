import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

@MainActor @Suite struct ConfigurationTests {
    func row(_ key: String, group: String = "voice", type: String = "string", source: String = "default",
             value: JSONValue? = nil, description: String = "", secret: Bool = false,
             title: String? = nil, groupTitle: String? = nil) -> ConfigRow {
        ConfigRow(key: key, group: group, title: title, groupTitle: groupTitle, description: description, source: source, type: type,
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
        #expect(groups[0].title == "Phone", "an older CLI's group gets this build's title")
    }

    @Test func groupsTakeTheCLIsTitleWhenItGivesOne() {
        let groups = ConfigWording.groups([row("LLM_SERVER_PORT", group: "local", groupTitle: "Local and self-hosted models")])
        #expect(groups.map(\.title) == ["Local and Self-Hosted Models"], "in title case, as a section title is")
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

    @Test func searchMatchesTheTitleToo() {
        let rows = [row("LLM_SERVER_PORT", description: "The port.", title: "Model server port"),
                    row("DATA_DIR", description: "Where the memory lives", title: "Data folder")]
        #expect(ConfigWording.filter(rows, by: "server port").map(\.key) == ["LLM_SERVER_PORT"])
        #expect(ConfigWording.filter(rows, by: "data_dir").map(\.key) == ["DATA_DIR"])
    }

    @Test func aRefusalNamesTheSettingByItsTitle() {
        let silence = row("VAD_SILENCE_MS", title: "Silence before a turn ends (ms)")
        #expect(ConfigWording.errorText("VAD_SILENCE_MS Input should be at most 10000", for: silence)
            == "Silence before a turn ends (ms): input should be at most 10000.")
        #expect(ConfigWording.errorText("the store is locked", for: silence) == "The store is locked.")
        #expect(ConfigWording.errorText("VAD_SILENCE_MS Input should be at most 10000", for: row("VAD_SILENCE_MS"))
            == "VAD_SILENCE_MS Input should be at most 10000.", "no title: the CLI's sentence as written")
    }

    @Test func validationSaysWhatToDo() {
        let silence = row("VAD_SILENCE_MS", type: "int", title: "Silence before a turn ends (ms)")
        #expect(ConfigWording.errorText("VAD_SILENCE_MS Input should be a valid integer, unable to parse", for: silence)
            == "Enter a whole number.")
        #expect(ConfigWording.errorText("VAD_THRESHOLD Input should be a valid number", for: silence) == "Enter a number.")
        #expect(ConfigWording.invalid("12oo", type: "int") == "Enter a whole number.")
        #expect(ConfigWording.invalid("1.5", type: "int") == "Enter a whole number.")
        #expect(ConfigWording.invalid("1200", type: "int") == nil)
        #expect(ConfigWording.invalid("0.5", type: "float") == nil)
        #expect(ConfigWording.invalid("half", type: "float") == "Enter a number.")
        #expect(ConfigWording.invalid("anything", type: "string") == nil)
        let refusal = KeryxError.failed(command: "keryx config set", exitCode: 1,
                                        message: "VAD_SILENCE_MS Input should be a valid integer")
        #expect(ConfigWording.errorText(refusal, for: silence) == "Enter a whole number.")
    }

    @Test func choicesReadAsWords() {
        let agents = row("AGENTS_ENABLED", group: "agents", type: "list")
        #expect(["claude", "codex", "local"].map { ConfigWording.choiceTitle($0, for: agents) }
            == ["Claude", "Codex", "Local model"])
        let noise = row("NOISE_REDUCTION", type: "choice")
        #expect(ConfigWording.choiceTitle("near_field", for: noise) == "Near field")
        #expect(ConfigWording.choiceTitle("anthropic-messages", for: noise) == "Anthropic messages")
        #expect(ConfigWording.choiceTitle("24h", for: noise) == "24h")
    }

    @Test func aChecklistKeepsOneOnAndTheChoicesOrder() {
        var agents = row("AGENTS_ENABLED", group: "agents", type: "list", value: .array([]))
        agents.choices = ["claude", "codex", "local"]
        #expect(ConfigWording.isChecklist(agents))
        #expect(!ConfigWording.isChecklist(row("ALLOWED_CALLERS", type: "list")))
        #expect(ConfigWording.checked(.array([]), fallback: "claude") == ["claude"], "blank is the default alone")
        #expect(ConfigWording.checked(.array([.string("local")]), fallback: "claude") == ["local"])
        #expect(ConfigWording.toggling("claude", on: true, in: ["local"], choices: agents.choices!) == "claude,local")
        #expect(ConfigWording.toggling("local", on: false, in: ["claude", "local"], choices: agents.choices!) == "claude")
    }

    @Test func secretsAndThePINSayWhetherTheyAreSet() {
        let key = row("OPENAI_API_KEY", source: "secrets.toml", secret: true)
        #expect(ConfigWording.secretCaption(key) == "Set · from secrets.toml")
        #expect(ConfigWording.secretCaption(row("ANTHROPIC_API_KEY", secret: true)) == "Not set")
        let pin = row("KERYX_PIN", source: "KERYX_HOME/pin", secret: true)
        #expect(ConfigWording.pinCaption(pin, status: nil) == "Set", "until keryx pin answers")
        #expect(ConfigWording.pinCaption(pin, status: PreviewData.pinFile) == "Set · in KERYX_HOME/pin")
    }

    @Test func theTooltipHasTheKey() {
        let mode = row("VAD_MODE", description: "How `semantic` decides")
        #expect(ConfigWording.tooltip(mode) == "How semantic decides\n\nVAD_MODE")
        #expect(Wording.titleCase("Owner, callers and PIN") == "Owner, Callers and PIN")
    }

    @Test func aFieldIsSentOnceAndARefusalIsNotResentUnchanged() {
        #expect(ConfigWording.shouldCommit("1300", stored: "1200", writing: false, refused: nil))
        #expect(!ConfigWording.shouldCommit("1200", stored: "1200", writing: false, refused: nil), "unchanged")
        #expect(!ConfigWording.shouldCommit("1300", stored: "1200", writing: true, refused: nil),
                "the blur that disabling the field causes must not send it again")
        #expect(!ConfigWording.shouldCommit("12oo", stored: "1200", writing: false, refused: "12oo"),
                "a refused value waits for an edit")
        #expect(ConfigWording.shouldCommit("1250", stored: "1200", writing: false, refused: "12oo"))
    }

    @Test func onlyASecretAsksBeforeItIsRemoved() {
        let key = row("OPENAI_API_KEY", source: "secrets.toml", secret: true, title: "OpenAI API key")
        #expect(ConfigWording.resetNeedsConfirmation(key))
        #expect(!ConfigWording.resetNeedsConfirmation(row("VAD_SILENCE_MS", source: "config.toml")))
        #expect(ConfigWording.removalTitle(key) == "Remove OpenAI API key?")
        #expect(ConfigWording.secretRemovalMessage.contains("paste it in again"))
    }

    @Test func configurationAnswersThePinAndTabRequests() {
        #expect(ConfigWording.answer(.setPin, pinTab: "general")! == ("general", .pin))
        #expect(ConfigWording.answer(.setPin, pinTab: nil)! == ("general", .pin), "before the settings are read")
        #expect(ConfigWording.answer(.configure(tab: "agents"), pinTab: "general")! == ("agents", nil))
        #expect(ConfigWording.answer(.show(.phone), pinTab: "general") == nil)
        #expect(ConfigWording.answer(.signIn(.claude), pinTab: "general")! == ("agents", .signIn(.claude)))
        #expect(ConfigWording.answer(.signIn(.gmail), pinTab: "general") == nil, "Integrations answers that")
    }

        @Test func twilioTokenIsChangedWithItsSID() {
        #expect(ConfigWording.isTwilioToken(row("TWILIO_AUTH_TOKEN", secret: true)))
        #expect(!ConfigWording.isTwilioToken(row("OPENAI_API_KEY", secret: true)))
    }

        @Test func thePINOffersNoWriteAndIsStillFound() {
        let pin = row("KERYX_PIN", group: "owner", source: "KERYX_HOME/pin",
                      description: "The PIN a caller gives.", secret: true, title: "Phone PIN")
        #expect(ConfigWording.isPIN(pin))
        #expect(!ConfigWording.offersWrites(pin), "no Reset to Default: the PIN sheet is its one write")
        #expect(ConfigWording.offersWrites(row("OPENAI_API_KEY", source: "secrets.toml", secret: true)))
        #expect(!ConfigWording.offersWrites(row("PUBLIC_HOST", source: "environment")))
        #expect(ConfigWording.filter([pin, row("DATA_DIR")], by: "pin").map(\.key) == ["KERYX_PIN"])
        #expect(PreviewData.config.contains { ConfigWording.isPIN($0) && $0.secret && $0.set })
    }

    @Test func theEnvironmentNoteNamesTheSettingByItsTitle() {
        let host = row("PUBLIC_HOST", source: "environment", title: "Public hostname")
        #expect(ConfigWording.environmentHelp(host)
            == "Public hostname is set in the environment as PUBLIC_HOST, which wins over the store")
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

    @Test func aDescriptionSaysTheSettingsItNamesByTheirTitles() {
        let rows = [row("AGENT_BACKEND", title: "Default agent"),
                    row("AGENTS_ENABLED", description: "Blank is `AGENT_BACKEND` alone.", title: "Enabled agents")]
        let titles = Wording.titles(rows)
        #expect(ConfigWording.described(rows[1].description, titles: titles) == "Blank is “Default agent” alone.")
        #expect(ConfigWording.tooltip(rows[1]) == "Blank is AGENT_BACKEND alone.\n\nAGENTS_ENABLED",
                "the tooltip keeps the description as written")
        #expect(ConfigWording.described("Its own `CODEX_HOME`.", titles: titles) == "Its own CODEX_HOME.")
    }

    @Test func descriptionsDropTheirBackticksForTooltips() {
        #expect(ConfigWording.plain("Unless `VOICE_BASE_URL` names one") == "Unless VOICE_BASE_URL names one")
    }

    @Test func theScreenIsInTheCatalogInEveryState() {
        let ids = Set(ScreenCatalog.entries().map(\.id))
        for id in ["configuration", "configuration-search", "configuration-search-tabs", "configuration-search-none",
                   "configuration-write-error", "configuration-needs-restart", "configuration-secret-sheet",
                   "configuration-voice-advanced", "configuration-system-advanced",
                   "configuration-general", "configuration-voice", "configuration-agents", "configuration-local",
                   "configuration-phone", "configuration-approvals", "configuration-integrations",
                   "configuration-system", "window-configuration"] {
            #expect(ids.contains(id), "missing \(id)")
        }
    }
}
