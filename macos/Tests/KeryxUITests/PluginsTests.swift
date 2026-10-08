import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// The Plugins screen: each plugin's state, the account it waits for, its fields as typed,
/// and the owner's own tools apart from the plugins'.
@MainActor @Suite struct PluginsTests {
    func plugin(_ name: String) -> Plugin { PreviewData.plugins.plugin(name)! }

    @Test func catalogHasEveryState() {
        let ids = Set(ScreenCatalog.entries().map(\.id))
        for id in ["plugins-populated", "plugins-all-on", "plugins-failure", "plugins-own-tools",
                   "plugins-no-own-tools", "plugins-error", "window-plugins"] {
            #expect(ids.contains(id), "missing \(id)")
        }
    }

    /// Minor 4: a plugin field hands on what was typed once — Return, then the blur and the
    /// section switch after it, send nothing more — and a refused value is not sent again on
    /// every blur until it is edited, as a Configuration row's `shouldCommit` holds it.
    @Test func aPluginFieldSendsWhatWasTypedOnce() {
        #expect(PluginsWording.shouldCommit("40", stored: "30", sent: nil))
        #expect(!PluginsWording.shouldCommit("40", stored: "30", sent: "40"), "in flight, or refused")
        #expect(PluginsWording.shouldCommit("45", stored: "30", sent: "40"), "edited since")
        #expect(!PluginsWording.shouldCommit("30", stored: "30", sent: nil), "unchanged")
    }

    @Test func statusSaysOnOffOrWhyNot() {
        #expect(PluginsWording.status(plugin("send_to_slack")) == (.ok, "On"))
        #expect(PluginsWording.status(plugin("check_email")) == (.warning, "Not ready"))
        #expect(PluginsWording.status(plugin("check_billing")) == (.warning, "Not ready"))
        #expect(PluginsWording.status(plugin("cluster_stats")) == (.inactive, "Off"))
        #expect(PluginsWording.detail(plugin("send_to_slack"), assistant: "Lyra") == "Lyra can use it from the next call.")
        #expect(PluginsWording.detail(plugin("check_email"), assistant: "Lyra") == "Connect Gmail in Integrations first.")
        #expect(PluginsWording.detail(plugin("cluster_stats"), assistant: nil) == "Needs a cluster host.")
    }

    @Test func aMissingAccountIsNamedUntilOneIsConnected() {
        let integrations = PreviewData.integrations
        let email = PluginsWording.missingAccounts(plugin("check_email"), integrations: integrations)
        #expect(email.map(\.id) == ["gmail"])
        #expect(PluginsWording.connectFirst(email) == "Connect Gmail…")
        let billing = PluginsWording.missingAccounts(plugin("check_billing"), integrations: integrations)
        #expect(billing.map(\.id) == ["openai-admin", "anthropic-admin"])
        #expect(PluginsWording.connectFirst(billing) == "Open Integrations")
        #expect(PluginsWording.missingAccounts(plugin("send_to_slack"), integrations: integrations).isEmpty,
                "Slack is connected")
        #expect(PluginsWording.missingAccounts(plugin("cluster_stats"), integrations: integrations).isEmpty)
        #expect(PluginsWording.missingAccounts(plugin("check_email"), integrations: nil).isEmpty)
        #expect(PluginsWording.connectFirst([]) == nil)

        // Either admin key will do.
        var one = integrations
        one.integrations[one.integrations.firstIndex { $0.id == "anthropic-admin" }!].connected = true
        #expect(PluginsWording.missingAccounts(plugin("check_billing"), integrations: one).isEmpty)
    }

    @Test func fieldsAreParsedAsTheirTypes() {
        #expect(PluginsWording.parse(" 60 ", as: .float) == .success("60"))
        #expect(PluginsWording.parse("12.5", as: .float) == .success("12.5"))
        #expect(PluginsWording.parse("60.0", as: .float) == .success("60"))
        #expect((try? PluginsWording.parse("sixty", as: .float).get()) == nil)
        #expect((try? PluginsWording.parse("-1", as: .float).get()) == nil)
        #expect((try? PluginsWording.parse("inf", as: .float).get()) == nil)
        #expect(PluginsWording.parse("3", as: .int) == .success("3"))
        #expect((try? PluginsWording.parse("3.5", as: .int).get()) == nil)
        #expect(PluginsWording.parse("true", as: .bool) == .success("true"))
        #expect(PluginsWording.parse("off", as: .bool) == .success("false"))
        #expect(PluginsWording.parse("  D0ABC ", as: .string) == .success("D0ABC"))
        #expect(PluginsWording.parse("", as: .string) == .success(""), "a word may be cleared")
        #expect((try? PluginsWording.parse("a\nb", as: .string).get()) == nil)
    }

    @Test func valuesReadAsTheEditorHoldsThem() {
        // Numbers decode as Double; a whole one reads without ".0".
        let budget = plugin("check_billing").fields.first { $0.key == "monthly_budget" }!
        #expect(budget.value == .number(60))
        #expect(PluginsWording.text(budget.value) == "60")
        #expect(PluginsWording.text(.number(20.0)) == "20")
        #expect(PluginsWording.text(.number(12.5)) == "12.5")
        #expect(PluginsWording.text(.bool(true)) == "true")
        #expect(PluginsWording.table(.object(["cluster": .string("gpu")])) == ["cluster": "gpu"])
        #expect(PluginsWording.table(.object([:])).isEmpty)
        #expect(PluginsWording.choiceTitle("openai") == "OpenAI")
        #expect(PluginsWording.choiceTitle("low") == "Low")
    }

    @Test func importantFieldsComeFirst() {
        let split = PluginsWording.split(plugin("check_billing").fields)
        #expect(split.important.map(\.key) == ["provider", "monthly_budget"])
        #expect(split.more.map(\.key) == ["openai_project_id", "openai_api_key_id", "anthropic_workspace_id"])
    }

    @Test func aDraftGoesWithTheInstall() {
        var draft = PluginDraft()
        #expect(draft.isEmpty)
        #expect(draft.values.isEmpty)
        draft.settings["channel_id"] = "D0ABC"
        draft.clusters = ["cluster": "gpu"]
        #expect(draft.values == PluginValues(settings: ["channel_id": "D0ABC"], clusters: ["cluster": "gpu"]))
    }

    @Test func onlyHostsWithAControlMasterAreOffered() {
        let hosts = PreviewData.sshHosts
        #expect(PluginsWording.clusterHosts(hosts, taken: []).map(\.alias) == ["cluster"])
        #expect(PluginsWording.clusterHosts(hosts, taken: ["cluster"]).isEmpty)
        #expect(PluginsWording.clusterHosts(nil, taken: []).isEmpty)
        #expect(PluginsWording.isBareWord("gpu-a100"))
        #expect(!PluginsWording.isBareWord("gpu a100"))
        #expect(!PluginsWording.isBareWord(""))
        #expect(!PluginsWording.isBareWord("gpu;rm"))
    }

    @Test func theOwnersToolsLeaveThePluginsOut() {
        let own = PluginsWording.ownTools(PreviewData.tools, plugins: PreviewData.plugins)
        #expect(own.tools.map(\.name) == ["open_garage", "tides"])
        #expect(own.refused.map(\.file) == ["broken.py"])
        // Without the plugins read, nothing is left out.
        #expect(PluginsWording.ownTools(PreviewData.tools, plugins: nil).tools.count == 4)
    }

    @Test func aToolSaysWhatItDoesInOneSentence() {
        #expect(PluginsWording.firstSentence("Opens the garage door. Say that it is opening, and nothing else.")
            == "Opens the garage door.")
        #expect(PluginsWording.firstSentence("No full stop here") == "No full stop here")
        #expect(PluginsWording.firstSentence("v1.2 works. Then more.") == "v1.2 works.")
        #expect(PluginsWording.refusal("not signed in to Gmail: `keryx auth login gmail`") == "Not signed in to Gmail.")
        #expect(PluginsWording.refusal("ModuleNotFoundError: No module named 'x'") == "Couldn't load.")
    }

    @Test func aFailureIsTheAppsSentenceAndKeryxsWordsAreDetails() throws {
        let slack = try #require(PreviewData.plugins.plugin("send_to_slack"))
        let refusal = KeryxError.failed(command: "keryx plugins install", exitCode: 1,
                                        message: "send_to_slack was not turned on: channel_id is empty")
        #expect(PluginsWording.failure(refusal, .on, plugin: slack)
            == StreamFailure("Slack wasn't turned on.", detail: "send_to_slack was not turned on: channel_id is empty"))
        #expect(PluginsWording.failure(refusal, .off, plugin: slack).message == "Slack wasn't turned off.")
        #expect(PluginsWording.failure(refusal, .settings, plugin: slack).message == "Slack's settings weren't saved.")
        let older = KeryxError.failed(command: "keryx plugins set", exitCode: 2, message: "No such command 'set'.")
        #expect(PluginsWording.failure(older, .settings, plugin: slack).message == KeryxError.olderKeryx)
    }

    @Test func turningOffAsksFirst() {
        let question = PluginsWording.turnOffQuestion(plugin("send_to_slack"), assistant: "Lyra")
        #expect(question.title == "Turn Off Slack?")
        #expect(question.message.hasPrefix("Lyra stops offering it from the next call."))
        #expect(question.button == "Turn Off")
    }
}
