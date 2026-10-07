import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// The Integrations screen and the sign-in sheets: what each card connects and
/// disconnects with, what it asks first, and which stage a sheet is in.
@MainActor @Suite struct IntegrationsTests {
    func card(_ id: String, _ report: IntegrationsReport = PreviewData.integrations) -> Integration {
        report.integration(id)!
    }

    func config(_ values: [String: JSONValue]) -> [ConfigRow] {
        PreviewData.config.map { row in
            guard let value = values[row.key] else { return row }
            var row = row
            row.value = value
            row.set = true
            return row
        }
    }

    @Test func catalogHasEveryState() {
        let ids = Set(ScreenCatalog.entries().map(\.id))
        for id in ["integrations-populated", "integrations-connected", "integrations-partial", "integrations-failure",
                   "integrations-error", "window-integrations", "signin-claude-ready", "signin-claude-browser",
                   "signin-claude-code", "signin-claude-checking", "signin-claude-done", "signin-codex-code",
                   "signin-gmail-needs-client", "signin-gmail-client-chosen", "signin-gmail-address",
                   "signin-gmail-address-wrong", "signin-gmail-address-ready", "signin-gmail-not-sent",
                   "signin-gmail-client-saved", "signin-refused-link",
                   "signin-google-workspace-waiting", "signin-google-no-client", "signin-failed", "signin-cancelling",
                   "signin-busy"] {
            #expect(ids.contains(id), "missing \(id)")
        }
    }

    // MARK: - Connecting and disconnecting

    @Test func eachCardConnectsItsOwnWay() {
        #expect(IntegrationsWording.connect(card("google")) == .signIn(.googleWorkspace))
        #expect(IntegrationsWording.connect(card("gmail")) == .signIn(.gmail))
        #expect(IntegrationsWording.connect(card("github")) == .issueReports)
        #expect(IntegrationsWording.connect(card("slack")) == .secret(card("slack").secrets[0]))
        #expect(IntegrationsWording.connect(card("openai-admin")) == .secret(card("openai-admin").secrets[0]))
    }

    @Test func disconnectUnsetsTheCardsSettingsAndSignsOutOfGoogle() {
        #expect(IntegrationsWording.disconnect(card("google"))
            == IntegrationDisconnect(unset: ["GOOGLE_WORKSPACE_MCP", "USER_GOOGLE_EMAIL"], signOut: .googleWorkspace))
        #expect(IntegrationsWording.disconnect(card("gmail")) == IntegrationDisconnect(unset: [], signOut: .gmail))
        #expect(IntegrationsWording.disconnect(card("slack"))
            == IntegrationDisconnect(unset: ["SLACK_BOT_TOKEN"], signOut: nil))
        // Turning reports off keeps a repository of the owner's choosing.
        #expect(IntegrationsWording.disconnect(card("github"))
            == IntegrationDisconnect(unset: ["ISSUE_REPORTING"], signOut: nil))
    }

    @Test func disconnectIsOfferedWhenSomethingIsStored() {
        #expect(IntegrationsWording.canDisconnect(card("slack"), config: nil), "its token is set")
        #expect(!IntegrationsWording.canDisconnect(card("openai-admin"), config: PreviewData.config))
        #expect(!IntegrationsWording.canDisconnect(card("google"), config: PreviewData.config))
        #expect(!IntegrationsWording.canDisconnect(card("google"), config: nil), "unknown until the settings are read")
        // Turned on, and not signed in: half there, so it can be undone.
        let on = config(["GOOGLE_WORKSPACE_MCP": .bool(true)])
        #expect(IntegrationsWording.canDisconnect(card("google"), config: on))
        #expect(IntegrationsWording.tone(card("google"), config: on) == .warning)
        #expect(IntegrationsWording.tone(card("google"), config: PreviewData.config) == .inactive)
        #expect(IntegrationsWording.tone(card("slack"), config: nil) == .ok)
    }

    @Test func issueReportsAreReadFromTheSettings() {
        #expect(IntegrationsWording.issueReportsOn(nil) == nil)
        #expect(IntegrationsWording.issueReportsOn(PreviewData.config) == false)
        #expect(IntegrationsWording.issueReportsOn(config(["ISSUE_REPORTING": .bool(true)])) == true)
        #expect(IntegrationsWording.issueRepo(config(["ISSUE_REPO": .string("ada/keryx")])) == "ada/keryx")
        #expect(IntegrationsWording.issueRepo(config(["ISSUE_REPO": .string("")])) == nil)
    }

    // MARK: - Words

    @Test func usedByNamesThePluginsByTheirTitles() {
        let plugins = PreviewData.plugins
        #expect(IntegrationsWording.usedBy([], plugins: plugins) == nil)
        #expect(IntegrationsWording.usedBy(["check_email"], plugins: plugins) == "The Email answers plugin")
        #expect(IntegrationsWording.usedBy(["check_email", "check_billing"], plugins: plugins)
            == "The Email answers and Billing plugins")
        #expect(IntegrationsWording.usedBy(["check_email"], plugins: nil) == "The check_email plugin")
    }

    @Test func disconnectSaysWhatGoesAndWhatStops() {
        let google = IntegrationsWording.disconnectQuestion(card("google"), plugins: PreviewData.plugins)
        #expect(google.title == "Disconnect Google for agents?")
        #expect(google.message.contains("stored sign-in is removed"))
        #expect(google.message.contains("client file stays"))
        let slack = IntegrationsWording.disconnectQuestion(card("slack"), plugins: PreviewData.plugins)
        #expect(slack.message == "The Slack bot token is removed from secrets.toml. "
            + "The Slack plugin stops working until you connect it again.")
        #expect(slack.button == "Disconnect")
        let github = IntegrationsWording.disconnectQuestion(card("github"), plugins: PreviewData.plugins)
        #expect(github.title == "Turn Off Issue Reports?")
        #expect(github.button == "Turn Off")
    }

    @Test func turningOnIssueReportsSaysTheyArePublic() {
        let question = IntegrationsWording.issueReportsQuestion(repo: "ada/keryx")
        #expect(question.message.contains("on ada/keryx, which anyone can read"))
        #expect(question.message.contains("never your number, your PIN"))
        #expect(IntegrationsWording.issueReportsQuestion(repo: nil).message.contains("Keryx's own repository"))
    }

    @Test func aGoogleClientIsAskedForWhenNoneIsSaved() {
        #expect(PreviewData.integrations.integration("gmail")?.clientSaved == false)
        #expect(PreviewData.integrations.integration("slack")?.clientSaved == nil)
        #expect(GoogleClient.saved(PreviewData.integrations) == false)
        #expect(GoogleClient.needsFile(PreviewData.integrations), "the sandbox has none")
        #expect(GoogleClient.saved(nil) == nil)
        #expect(!GoogleClient.needsFile(nil))

        // `client_saved` decides, whatever `why` says.
        var saved = PreviewData.integrations
        for index in saved.integrations.indices where saved.integrations[index].clientSaved != nil {
            saved.integrations[index].clientSaved = true
        }
        #expect(GoogleClient.saved(saved) == true)
        #expect(!GoogleClient.needsFile(saved))

        // An older CLI says nothing of it: `why` is read, and silence is not knowing.
        var older = PreviewData.integrations
        for index in older.integrations.indices { older.integrations[index].clientSaved = nil }
        #expect(GoogleClient.saved(older) == false)
        for index in older.integrations.indices { older.integrations[index].why = nil }
        #expect(GoogleClient.saved(older) == nil)
        #expect(!GoogleClient.needsFile(older))
        #expect(GoogleClient.isMissing("there is no Google OAuth client yet: download one … --client-file"))
        #expect(!GoogleClient.isMissing("Signed in to Gmail, read-only."))
        #expect(SignInWording.failure("there is no Google OAuth client yet: pass it with --client-file")
            .hasPrefix("There's no Google OAuth client yet."))
        #expect(SignInWording.failure("the sign-in took too long") == "The sign-in took too long.")
    }

    // MARK: - Sign-in stages

    typealias Link = StreamRun<SignedIn>.Link

    func stage(_ kind: SignInProvider, _ phase: StreamRun<SignedIn>.Phase?, signingIn: SignInProvider? = nil,
               startedHere: Bool = true, sent: Bool = false) -> SignInStage {
        SignInStage.of(kind, phase: phase, signingIn: signingIn ?? kind, startedHere: startedHere, sent: sent)
    }

    @Test func aSheetIsReadyUntilItsRunStarts() {
        #expect(stage(.claude, nil) == .ready)
        #expect(stage(.claude, nil, startedHere: false) == .ready)
        // A run of its own kind that this sheet did not start and that has ended is not its.
        #expect(stage(.claude, .failed("old"), startedHere: false) == .ready)
        #expect(stage(.claude, .finished(SignedIn(name: "claude", message: "ok")), startedHere: false) == .ready)
    }

    @Test func anotherSignInHoldsTheRun() {
        let link = Link(url: "https://auth.openai.com/codex/device", code: "K7QF")
        #expect(stage(.claude, .running(.init(link: link)), signingIn: .codex, startedHere: false)
            == .busyElsewhere(.codex))
        #expect(stage(.claude, .cancelling, signingIn: .codex, startedHere: false) == .busyElsewhere(.codex))
        // The same kind, still running, is picked up again by a sheet opened later.
        #expect(stage(.codex, .running(.init(link: link)), startedHere: false) == .deviceCode(link))
    }

    @Test func claudeAsksForItsCodeOnlyOnceTheCLIDoes() {
        let link = Link(url: "https://claude.com/cai/oauth/authorize?state=x")
        #expect(stage(.claude, .running(.init(message: "Starting…"))) == .starting("Starting…"))
        #expect(stage(.claude, .running(.init(link: link))) == .browser(link))
        #expect(stage(.claude, .running(.init(link: link, wantsCode: true))) == .wantsCode(link))
        #expect(stage(.claude, .running(.init(link: link)), sent: true) == .checking)
    }

    @Test func gmailAsksForTheAddressOnceThePageIsOpen() {
        let link = Link(url: "https://accounts.google.com/o/oauth2/auth")
        #expect(stage(.gmail, .running(.init())) == .starting(StreamRun<SignedIn>.starting))
        #expect(stage(.gmail, .running(.init(link: link))) == .wantsAddress(link))
        #expect(stage(.gmail, .running(.init(link: link)), sent: true) == .checking)
        #expect(stage(.googleWorkspace, .running(.init(link: link))) == .browser(link))
    }

    @Test func endingsAreTheirOwnStages() {
        let done = SignedIn(name: "codex", message: "Signed in to Codex with ChatGPT.")
        #expect(stage(.codex, .finished(done)) == .finished(done))
        #expect(stage(.codex, .failed("Codex's sign-in did not finish")) == .failed("Codex's sign-in did not finish"))
        #expect(stage(.codex, .cancelling) == .cancelling)
        #expect(!SignInStage.cancelling.isRunning)
        #expect(SignInStage.checking.isRunning)
        #expect(SignInWording.finished(SignedIn(name: "claude", message: "", stored: "CLAUDE_CODE_OAUTH_TOKEN")) != nil)
        #expect(SignInWording.finished(done) == nil)
    }

    @Test func onlyATrustedPageIsOpened() {
        #expect(SignInWording.page(Link(url: "https://accounts.google.com/o/oauth2/auth"))?.host
            == "accounts.google.com")
        #expect(SignInWording.page(Link(url: "https://auth.openai.com/codex/device", code: "K7QF"))?.host
            == "auth.openai.com")
        #expect(SignInWording.page(Link(url: "https://auth.openai.com.example.net/codex/device")) == nil,
                "a lookalike host")
        #expect(SignInWording.page(Link(url: "https://example.com/oauth")) == nil)
        #expect(SignInWording.page(Link(url: "http://accounts.google.com/")) == nil)
        #expect(SignInWording.page(Link(url: "file:///etc/passwd")) == nil)
        #expect(SignInWording.page(Link(url: "javascript:alert(1)")) == nil)
        #expect(SignInWording.page(nil) == nil)
    }

    @Test func onlyGooglesLandingAddressIsSent() {
        let landed = "http://localhost:1/?state=s&code=4%2F0Ab&scope=https://www.googleapis.com/auth/gmail.readonly"
        #expect(SignInWording.landingAddress("  \(landed)\n") == landed)
        #expect(SignInWording.landingAddress("http://127.0.0.1:8080/?code=c") != nil)
        #expect(SignInWording.landingAddress("http://[::1]:1/?code=c") != nil)
        #expect(SignInWording.landingAddress("http://localhost:1/?error=access_denied") != nil, "a refusal is an answer")
        #expect(SignInWording.landingAddress("http://localhost:1/") == nil, "no code")
        #expect(SignInWording.landingAddress("http://localhost:1/?state=s") == nil)
        #expect(SignInWording.landingAddress("https://localhost:1/?code=c") == nil, "Google lands on http")
        #expect(SignInWording.landingAddress("http://example.com/?code=c") == nil)
        #expect(SignInWording.landingAddress("http://localhost.example.com/?code=c") == nil)
        #expect(SignInWording.landingAddress("http://user@localhost:1/?code=c") == nil)
        #expect(SignInWording.landingAddress("https://mail.google.com/mail/u/0/#inbox") == nil)
        #expect(SignInWording.landingAddress("4/0AbCdEf") == nil, "the code alone")
        #expect(SignInWording.landingAddress("http://localhost:1/?code=c\nhttp://localhost:1/?code=d") == nil)
        #expect(SignInWording.landingAddress("") == nil)
    }

    @Test func whatIsPastedGoesAsOneLine() {
        #expect(SignInWording.line("  abc#def \n") == "abc#def")
        #expect(SignInWording.line("http://localhost:1/?code=c&state=s") == "http://localhost:1/?code=c&state=s")
        #expect(SignInWording.line("one\ntwo") == "", "two lines are not one")
        #expect(SignInWording.line("   ") == "")
    }

    @Test func theSignInsHaveTheirNames() {
        #expect(SignInProvider.allCases.map(SignInWording.name) == ["Claude", "Codex", "Gmail", "Google for agents"])
        #expect(SignInWording.title(.googleWorkspace) == "Sign In to Google for Agents")
        #expect(SignInWording.busyElsewhere(.codex) == "A sign-in to Codex is still going. Finish or cancel it first.")
    }

    // MARK: - Quitting

    @Test func quittingDuringASignInAsksFirst() {
        func ask(_ name: String?, _ detail: String? = nil, downloading: Bool = false) -> Quitting.Question? {
            Quitting.question(downloading: downloading, settingUpVoice: false, signingIn: name, signInDetail: detail,
                              assistant: "Lyra")
        }
        #expect(ask(nil) == nil)
        let alone = ask("Claude", SignInWording.quitDetail(.claude))
        #expect(alone?.message == "A sign-in to Claude is still going. Quit anyway?")
        #expect(alone?.detail == "The sign-in stops, and nothing is saved. You can start it again later.")
        // Google for agents is turned on as its sign-in starts: something is kept.
        let workspace = SignInWording.quitDetail(.googleWorkspace)
        #expect(!workspace.contains("nothing is saved"))
        #expect(workspace.contains("stays turned on"))
        #expect(ask("Google for agents", workspace)?.detail == workspace)
        // With something else under way, one question names both.
        let both = ask("Gmail", downloading: true)
        #expect(both?.message == "A model is downloading and a sign-in to Gmail is still going. Quit anyway?")
        #expect(both?.detail == "Both stop. The download picks up where it left off, and the sign-in to Gmail "
            + "can be started again later.")
    }
}
