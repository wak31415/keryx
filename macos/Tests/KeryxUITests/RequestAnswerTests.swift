import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// Every request the Setup Assistant can make is answered by a screen of the main window:
/// the table the screens share (`RequestAnswer`), and each screen's own reading of it.
@MainActor @Suite struct RequestAnswerTests {
    /// One of each request, every provider and every section included.
    nonisolated static let requests: [AppRequest] =
        SidebarItem.allCases.map(AppRequest.show)
        + [.configure(tab: "agents"), .configure(tab: "general"), .setPin, .setUpVoiceHere,
           .connect(integration: "google"), .connect(integration: "github")]
        + SignInProvider.allCases.map(AppRequest.signIn)

    /// A switch with no `default`: a new `AppRequest` case fails to compile here until it
    /// is added to `requests` above and answered.
    static func isListed(_ request: AppRequest) -> Bool {
        switch request {
        case .show, .configure, .signIn, .connect, .setPin, .setUpVoiceHere: true
        }
    }

    /// The screens that would act on `request`, read exactly as each screen reads it.
    static func screensAnswering(_ request: AppRequest) -> Set<SidebarItem> {
        var screens: Set<SidebarItem> = []
        // ConfigurationView.answer
        if ConfigWording.answer(request, pinTab: "general") != nil { screens.insert(.configuration) }
        // IntegrationsView.answerRequest, with the cards read
        if let answer = IntegrationsWording.answer(request, cards: PreviewData.integrations.integrations),
           answer != .unknown, answer != .wait {
            screens.insert(.integrations)
        }
        let answer = RequestAnswer.of(request)
        // VoiceView and its This Mac section
        if answer == .voiceHere { screens.insert(.voice) }
        // ModelsView
        if answer == .models { screens.insert(.models) }
        // RootView: a section only shown
        if case let .shown(item) = answer { screens.insert(item) }
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

    @Test func googleSignInsAndConnectsAreIntegrations() {
        let cards = PreviewData.integrations.integrations
        #expect(IntegrationsWording.answer(.signIn(.gmail), cards: nil) == .open(.signIn(.gmail)),
                "a sheet needs no cards")
        #expect(IntegrationsWording.answer(.signIn(.googleWorkspace), cards: cards)
            == .open(.signIn(.googleWorkspace)))
        #expect(IntegrationsWording.answer(.connect(integration: "google"), cards: nil) == .wait)
        let google = cards.first { $0.id == "google" }!
        #expect(IntegrationsWording.answer(.connect(integration: "google"), cards: cards) == .connect(google))
        // Its own Connect: Google's is the sign-in, GitHub's the question about issue reports.
        #expect(IntegrationsWording.connect(google) == .signIn(.googleWorkspace))
        let github = cards.first { $0.id == "github" }!
        #expect(IntegrationsWording.connect(github) == .issueReports)
        #expect(IntegrationsWording.answer(.connect(integration: "nothing"), cards: cards) == .unknown)
        #expect(IntegrationsWording.answer(.connect(integration: "google"), cards: []) == .unknown,
                "a failed read: shown, and nothing more")
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

    @Test func eachAgentIsReadFromItsOwnCheck() {
        let report = doctor([
            ("coding agents", true, "claude by default; enabled: claude, codex"),
            ("Claude Code agent (default)", true, "/usr/local/bin/claude (2.1); stored subscription login"),
            ("Codex agent", false, "/usr/local/bin/codex; no login found — run codex login"),
        ])
        let claude = AgentSignInWording.state(.claude, doctor: report)
        #expect(claude == .signedIn("stored subscription login"))
        #expect(AgentSignInWording.status(claude) == "Signed In")
        #expect(AgentSignInWording.button(claude) == "Sign In Again…")
        #expect(AgentSignInWording.caption(claude) == "Stored subscription login")
        let codex = AgentSignInWording.state(.codex, doctor: report)
        #expect(codex == .notSignedIn)
        #expect(AgentSignInWording.status(codex) == "Not Signed In")
        #expect(AgentSignInWording.button(codex) == "Sign In…")
    }

    @Test func notInstalledNotEnabledAndNotYetRead() {
        let report = doctor([("Claude Code agent (default)", false, "not installed — uv sync --extra claude")])
        #expect(AgentSignInWording.state(.claude, doctor: report) == .notInstalled)
        #expect(AgentSignInWording.state(.codex, doctor: report) == .notEnabled)
        #expect(AgentSignInWording.state(.codex, doctor: nil) == .unknown)
        #expect(AgentSignInWording.button(.notEnabled) == "Sign In…", "signing in is never refused here")
    }

    @Test func thePreviewDoctorsClaudeIsNotSignedIn() {
        #expect(AgentSignInWording.state(.claude, doctor: PreviewData.doctor) == .notSignedIn)
    }
}
