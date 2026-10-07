import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// The CLI's fix ids (`keryx/fixes.py`), each a button: every id in its closed set maps to
/// one, and every fix the fixtures carry does.
@MainActor @Suite struct FixActionTests {
    /// `fixes.ACTIONS`, as the CLI writes them.
    nonisolated static let screens = [
        "pin", "service:install", "models:start", "models:choose", "voice:here", "voice:download",
        "phone:account", "phone:webhooks", "phone:tunnel", "migrate",
    ]
    /// `fixes.SIGNINS`.
    nonisolated static let signIns = ["claude", "codex", "gmail", "google-workspace"]
    /// One of each of the ids that take an argument, beside the sign-ins.
    nonisolated static let withArguments = [
        "secret:OPENAI_API_KEY", "secret:VOICE_API_KEY", "secret:LOCAL_AGENT_API_KEY", "setting:ALLOWED_CALLERS",
        "setting:AGENT_BACKEND", "setting:VOICE_BASE_URL", "setting:LOCAL_AGENT_MODEL", "setting:PROJECTS_ROOT",
        "plugin:check_email", "plugin:cluster_stats", "integration:google", "integration:slack",
        "integration:github", "integration:openai-admin",
    ]

    nonisolated static var everyId: [String] { screens + signIns.map { "signin:\($0)" } + withArguments }

    @Test(arguments: everyId)
    func everyIdTheCLIMayGiveIsAButton(_ id: String) throws {
        let fix = try #require(FixAction(id), "\(id) maps to nothing")
        let action = fix.action(SetupContext(config: PreviewData.config, pin: PreviewData.pinNone,
                                             service: PreviewData.service))
        #expect(!action.title.isEmpty)
        #expect(!action.title.contains("_"), "\(id): \(action.title) names a key, not a setting")
    }

    @Test func theNoArgumentIdsAreExactlyTheCLIsSet() {
        #expect(Set(FixAction.screens.keys) == Set(Self.screens))
    }

    @Test func everyFixTheFixturesCarryIsAButton() {
        var ids: [String] = PreviewData.doctor.checks.compactMap(\.fix)
        for plan in [PreviewData.setupPlan, PreviewData.setupPlanPartway, PreviewData.setupPlanReady] {
            ids += plan.steps.compactMap(\.fix) + plan.steps.flatMap(\.checks).compactMap(\.fix)
        }
        #expect(!ids.isEmpty)
        for id in ids { #expect(FixAction(id) != nil, "\(id)") }
    }

    @Test func nullAndUnknownIdsAreNoButton() {
        #expect(FixAction(nil) == nil)
        #expect(FixAction("doctor:fix") == nil)
        #expect(FixAction("signin:gemini") == nil)
        #expect(FixAction("secret:") == nil)
    }

    @Test func theButtonsSayWhatTheyDo() {
        let context = SetupContext(config: PreviewData.config, pin: PreviewData.pinNone)
        #expect(FixAction("signin:claude")?.action(context)
            == SetupAction(title: "Sign In to Claude…", effect: .request(.signIn(.claude))))
        #expect(FixAction("pin")?.action(context) == SetupAction(title: "Set PIN…", effect: .request(.setPin)))
        #expect(FixAction("pin")?.action(SetupContext(pin: PreviewData.pinFile)).title == "Change PIN…")
        #expect(FixAction("setting:ALLOWED_CALLERS")?.action(context).effect
            == .request(.setting(key: "ALLOWED_CALLERS")))
        #expect(FixAction("setting:VOICE_BASE_URL")?.action(context).effect == .request(.show(.voice)))
        #expect(FixAction("integration:github")?.action(context).title == "Connect GitHub…")
        #expect(FixAction("phone:webhooks")?.action(context).effect == .request(.pointHere))
        #expect(FixAction("secret:OPENAI_API_KEY")?.action(SetupContext()).title == "Add OpenAI API Key…")
    }

    @Test func theTunnelIsInstalledFromItsOwnPageOnly() throws {
        let unset = SetupContext(phoneTunnel: PreviewData.phoneTunnelUnset)
        let install = try #require(FixAction("phone:tunnel")?.action(unset))
        #expect(install.title == "How to Install…")
        guard case let .openURL(url) = install.effect else {
            Issue.record("not a page: \(install.effect)")
            return
        }
        #expect(url.host == "developers.cloudflare.com")
        // Installed: the Phone section, where its hostname is set.
        #expect(FixAction("phone:tunnel")?.action(SetupContext(phoneTunnel: PreviewData.phoneTunnel)).effect
            == .request(.show(.phone)))
        // A page that is not a tunnel's own download page is never opened.
        #expect(FixAction.tunnelPage("https://example.com/cloudflared") == nil)
        #expect(FixAction.tunnelPage("http://developers.cloudflare.com/") == nil)
    }
}
