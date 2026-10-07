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
        #expect(IntegrationsWording.disconnect(card("github"))
            == IntegrationDisconnect(unset: ["ISSUE_REPORTING", "ISSUE_REPO"], signOut: nil))
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
        #expect(GoogleClient.needsFile(PreviewData.integrations), "the sandbox has none")
        #expect(!GoogleClient.needsFile(nil))
        var signedIn = PreviewData.integrations
        for index in signedIn.integrations.indices { signedIn.integrations[index].why = nil }
        #expect(!GoogleClient.needsFile(signedIn))
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

    @Test func onlyAnHttpsPageIsOpened() {
        #expect(SignInWording.openable(Link(url: "https://accounts.google.com/o/oauth2/auth"))?.host
            == "accounts.google.com")
        #expect(SignInWording.openable(Link(url: "http://example.com")) == nil)
        #expect(SignInWording.openable(Link(url: "file:///etc/passwd")) == nil)
        #expect(SignInWording.openable(Link(url: "javascript:alert(1)")) == nil)
        #expect(SignInWording.openable(nil) == nil)
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
        #expect(Quitting.adding(signIn: nil, to: nil) == nil)
        let alone = Quitting.adding(signIn: "Claude", to: nil)
        #expect(alone?.message == "A sign-in to Claude is still going. Quit anyway?")
        let download = Quitting.question(downloading: true, settingUpVoice: false, assistant: "Lyra")
        let both = Quitting.adding(signIn: "Gmail", to: download)
        #expect(both?.message == download?.message)
        #expect(both?.detail.hasSuffix(" The sign-in to Gmail stops too.") == true)
        #expect(Quitting.adding(signIn: nil, to: download) == download)
    }
}
