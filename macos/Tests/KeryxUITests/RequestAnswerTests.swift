import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// Every request the setup wizard can make is answered by a screen of the main window:
/// the table the screens share (`RequestAnswer`), and each screen's own reading of it.
@MainActor @Suite struct RequestAnswerTests {
    /// One of each request, every provider and every section included.
    nonisolated static let requests: [AppRequest] =
        SidebarItem.allCases.map(AppRequest.show)
        + [.configure(tab: "agents"), .configure(tab: "general"), .setPin, .setUpVoiceHere,
           .connect(integration: "google"), .connect(integration: "github"),
           .setting(key: "ALLOWED_CALLERS"), .setting(key: "PROJECTS_ROOT"), .plugin(name: "check_email"),
           .phoneAccount, .pointHere]
        + SignInProvider.allCases.map(AppRequest.signIn)

    /// A switch with no `default`: a new `AppRequest` case fails to compile here until it
    /// is added to `requests` above and answered.
    static func isListed(_ request: AppRequest) -> Bool {
        switch request {
        case .show, .configure, .signIn, .connect, .setPin, .setUpVoiceHere, .setting, .plugin, .phoneAccount,
             .pointHere: true
        }
    }

    /// The screens that would act on `request`: each screen's own check.
    static func screensAnswering(_ request: AppRequest) -> Set<SidebarItem> {
        var screens: Set<SidebarItem> = []
        if ConfigurationView.answer(request, config: PreviewData.config) != nil { screens.insert(.configuration) }
        let read = Loadable(value: PreviewData.integrations)
        if let answer = IntegrationsView.answer(request, integrations: read), answer != .unknown, answer != .wait {
            screens.insert(.integrations)
        }
        if VoiceView.answers(request) { screens.insert(.voice) }
        if ModelsView.answers(request) { screens.insert(.models) }
        if PluginsView.answers(request) != nil { screens.insert(.plugins) }
        if PhoneView.answers(request) != nil { screens.insert(.phone) }
        if let item = RootView.answers(request) { screens.insert(item) }
        return screens
    }

    @Test(arguments: requests)
    func exactlyTheScreenItShowsAnswersIt(_ request: AppRequest) {
        #expect(Self.isListed(request))
        #expect(RequestAnswer.of(request).section == request.destination)
        #expect(Self.screensAnswering(request) == [request.destination])
    }

    @Test func theCodingAgentsSignInOnTheAgentsTab() {
        for provider in [SignInProvider.claude, .codex] {
            #expect(RequestAnswer.of(.signIn(provider)) == .configuration(tab: "agents", sheet: .signIn(provider)))
            let answer = ConfigWording.answer(.signIn(provider), pinTab: "general")
            #expect(answer?.tab == "agents" && answer?.sheet == .signIn(provider))
        }
    }

    @Test func googleSignInsAndConnectsAreIntegrations() throws {
        let cards = PreviewData.integrations.integrations
        #expect(IntegrationsWording.answer(.signIn(.gmail), cards: nil) == .open(.signIn(.gmail)),
                "a sheet needs no cards")
        #expect(IntegrationsWording.answer(.signIn(.googleWorkspace), cards: cards)
            == .open(.signIn(.googleWorkspace)))
        #expect(IntegrationsWording.answer(.connect(integration: "google"), cards: nil) == .wait)
        let google = try #require(cards.first { $0.id == "google" })
        #expect(IntegrationsWording.answer(.connect(integration: "google"), cards: cards) == .connect(google))
        // Its own Connect: Google's is the sign-in, GitHub's the question about issue reports.
        #expect(IntegrationsWording.connect(google) == .signIn(.googleWorkspace))
        let github = try #require(cards.first { $0.id == "github" })
        #expect(IntegrationsWording.connect(github) == .issueReports)
        #expect(IntegrationsWording.answer(.connect(integration: "nothing"), cards: cards) == .unknown)
        #expect(IntegrationsWording.answer(.connect(integration: "google"), cards: []) == .unknown)
        let failed = Loadable<IntegrationsReport>(error: .timedOut(command: "keryx integrations"))
        #expect(IntegrationsView.answer(.connect(integration: "google"), integrations: failed) == .unknown,
                "a failed read: shown, and nothing more")
        #expect(IntegrationsView.answer(.connect(integration: "google"), integrations: .idle) == .wait)
        #expect(IntegrationsWording.answer(.setPin, cards: cards) == nil)
    }
}

/// Claude's and Codex's rows on the Agents tab, as the doctor says.
@Suite struct AgentSignInTests {
    func doctor(_ checks: [(String, Bool, String)]) -> DoctorReport {
        var report = PreviewData.doctor
        report.checks = checks.map { name, ok, detail in
            DoctorCheck(name: name, detail: detail, severity: "hard", section: "agents",
                        state: ok ? "ok" : "missing", ok: ok, unset: !ok, title: nil, unknown: nil)
        }
        return report
    }

    // The shapes `doctor.py`'s `_agent_checks` writes.
    @Test func eachAgentIsReadFromItsOwnCheck() {
        let report = doctor([
            ("coding agents", true, "claude by default; enabled: claude, codex"),
            ("Claude Code agent (default)", true, "stored subscription login"),
            ("Codex agent", false, "not signed in"),
        ])
        let claude = AgentSignInWording.state(.claude, doctor: report)
        #expect(claude == .signedIn("stored subscription login"))
        #expect(AgentSignInWording.status(claude) == "Signed in")
        #expect(AgentSignInWording.button(claude) == "Sign In Again…")
        #expect(AgentSignInWording.caption(claude) == "Stored subscription login")
        let codex = AgentSignInWording.state(.codex, doctor: report)
        #expect(codex == .notSignedIn)
        #expect(AgentSignInWording.status(codex) == "Not signed in")
        #expect(AgentSignInWording.button(codex) == "Sign In…")
        let key = doctor([("Claude Code agent (default)", true, "ANTHROPIC_API_KEY set (pay per token)")])
        #expect(AgentSignInWording.caption(AgentSignInWording.state(.claude, doctor: key))
            == "ANTHROPIC_API_KEY set (pay per token)")
    }

    @Test func notInstalledNotEnabledAndNotYetRead() {
        let report = doctor([
            ("Claude Code agent (default)", false, "not installed"),
            ("Codex agent", false, "the codex program is missing"),
        ])
        #expect(AgentSignInWording.state(.claude, doctor: report) == .notInstalled)
        #expect(AgentSignInWording.state(.codex, doctor: report) == .notInstalled, "its program is missing")
        let claudeOnly = doctor([("Claude Code agent (default)", false, "not signed in")])
        #expect(AgentSignInWording.state(.codex, doctor: claudeOnly) == .notEnabled)
        #expect(AgentSignInWording.state(.codex, doctor: nil) == .unknown)
        #expect(AgentSignInWording.button(.notEnabled) == nil, "Enable instead")
        #expect(AgentSignInWording.button(.notInstalled) == nil, "nothing to sign in to")
        #expect(AgentSignInWording.button(.unknown) == nil)
    }

    @Test func enablingAddsTheAgentAsTheChecklistWould() throws {
        var row = try #require(PreviewData.config.first { $0.key == AgentSignInWording.enabledKey })
        #expect(AgentSignInWording.enabling(.codex, row: row, fallback: "claude") == "claude,codex,local")
        row.value = .null
        #expect(AgentSignInWording.enabling(.codex, row: row, fallback: "claude") == "claude,codex",
                "blank is the default agent alone, which stays")
        #expect(AgentSignInWording.notEnabled(.codex) == "Codex wasn't enabled.")
    }

    @Test func anOlderCLIsDetailStillReads() {
        let report = doctor([
            ("Claude Code agent (default)", true, "/usr/local/bin/claude (2.1); stored subscription login"),
            ("Codex agent", false, "codex CLI not found — npm install -g @openai/codex"),
        ])
        #expect(AgentSignInWording.state(.claude, doctor: report) == .signedIn("stored subscription login"))
        #expect(AgentSignInWording.state(.codex, doctor: report) == .notInstalled)
    }

    @Test func theFixIdsSayWhichAgentWantsASignIn() {
        func check(_ name: String, ok: Bool, fix: String?, hint: String? = nil) -> DoctorCheck {
            DoctorCheck(name: name, detail: ok ? "stored subscription login" : "whatever it says",
                        severity: "hard", section: "agents", state: ok ? "ok" : "missing", ok: ok, unset: !ok,
                        hint: hint, fix: fix)
        }
        var report = PreviewData.doctor
        report.checks = [check("Claude Code agent (default)", ok: false, fix: "signin:claude"),
                         check("Codex agent", ok: false, fix: nil, hint: "uv sync --extra codex")]
        #expect(AgentSignInWording.state(.claude, doctor: report) == .notSignedIn)
        #expect(AgentSignInWording.state(.codex, doctor: report) == .notInstalled, "no sign-in offered")
    }

    @Test func thePreviewDoctorsClaudeIsNotSignedIn() {
        #expect(AgentSignInWording.state(.claude, doctor: PreviewData.doctor) == .notSignedIn)
    }
}
