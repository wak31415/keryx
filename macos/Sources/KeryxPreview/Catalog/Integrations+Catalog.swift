import KeryxKit
import KeryxUI
import SwiftUI

extension ScreenCatalog {
    /// The Integrations section and the sign-in sheets (Task UD): the accounts as a sandbox
    /// has them (Slack's token set, the rest not), all connected, two half done (Google for
    /// agents on and not signed in, issue reports on and gh not signed in), a change that
    /// failed, a list that failed to load, and the window; then each sign-in sheet in each
    /// of its stages.
    @MainActor static var integrationsEntries: [Entry] {
        let screen = CGSize(width: 720, height: 1240)
        let switchedOn: [String: JSONValue] = ["ISSUE_REPORTING": .bool(true), "GOOGLE_WORKSPACE_MCP": .bool(true)]
        return [
            Entry("integrations-populated", size: screen) {
                IntegrationsView().environment(AppModel.preview())
            },
            Entry("integrations-connected", size: screen) {
                IntegrationsView().environment(AppModel.preview(client: PreviewClient(
                    config: .success(storing(switchedOn)), integrations: .success(allConnected))))
            },
            Entry("integrations-partial", size: screen) {
                IntegrationsView().environment(AppModel.preview(client: PreviewClient(
                    config: .success(storing(switchedOn)), integrations: .success(halfConnected))))
            },
            Entry("integrations-failure", size: CGSize(width: 720, height: 560)) {
                IntegrationsView(failures: ["google": StreamFailure("Google for agents wasn't turned off.",
                                                                     detail: "could not write config.toml (Permission denied)")])
                    .environment(AppModel.preview())
            },
            Entry("integrations-error", size: CGSize(width: 720, height: 420)) {
                IntegrationsView().environment(AppModel.preview(client: PreviewClient(
                    integrations: .failure(.timedOut(command: "keryx integrations --json")))))
            },
            Entry("window-integrations", size: windowSize) {
                RootView().environment(windowModel(on: .integrations))
            },
        ] + signInEntries
    }

    /// Each sign-in sheet in each stage, its run already where the stage is.
    @MainActor static var signInEntries: [Entry] {
        let short = CGSize(width: 480, height: 230)
        let tall = CGSize(width: 480, height: 300)
        let codex = StreamEvent<SignedIn>.open(url: "https://auth.openai.com/codex/device", code: "K7QF-2MXRA")
        return [
            Entry("signin-claude-ready", size: short) {
                sheet(SignInSheet(kind: .claude), AppModel.preview())
            },
            Entry("signin-claude-browser", size: short) {
                sheet(SignInSheet(kind: .claude, startedHere: true), signingIn(.claude, .holding([claudeOpen])))
            },
            Entry("signin-claude-code", size: tall) {
                sheet(SignInSheet(kind: .claude, startedHere: true),
                      signingIn(.claude, .holding([claudeOpen, .code])))
            },
            Entry("signin-claude-checking", size: short) {
                sheet(SignInSheet(kind: .claude, startedHere: true, sent: true),
                      signingIn(.claude, .holding([claudeOpen])))
            },
            Entry("signin-claude-done", size: short) {
                sheet(SignInSheet(kind: .claude, startedHere: true),
                      signingIn(.claude, .events([claudeOpen, .code, .done(SignedIn(
                          name: "claude", message: "Signed in to Claude with your subscription.",
                          stored: "CLAUDE_CODE_OAUTH_TOKEN"))])))
            },
            Entry("signin-codex-code", size: tall) {
                sheet(SignInSheet(kind: .codex, startedHere: true), signingIn(.codex, .holding([codex])))
            },
            Entry("signin-gmail-needs-client", size: tall) {
                sheet(SignInSheet(kind: .gmail), AppModel.preview())
            },
            Entry("signin-gmail-client-chosen", size: tall) {
                sheet(SignInSheet(kind: .gmail, startedHere: false,
                                  clientFile: "/Users/you/Downloads/client_secret_example.apps.googleusercontent.com.json"),
                      AppModel.preview())
            },
            Entry("signin-gmail-address", size: CGSize(width: 480, height: 330)) {
                sheet(SignInSheet(kind: .gmail, startedHere: true), signingIn(.gmail, .holding([googleOpen])))
            },
            Entry("signin-gmail-address-wrong", size: CGSize(width: 480, height: 380)) {
                sheet(SignInSheet(kind: .gmail, startedHere: true, typed: "https://mail.google.com/mail/u/0/#inbox"),
                      signingIn(.gmail, .holding([googleOpen])))
            },
            Entry("signin-gmail-address-ready", size: CGSize(width: 480, height: 350)) {
                sheet(SignInSheet(kind: .gmail, startedHere: true,
                                  typed: "http://localhost:1/?state=example&code=example&scope=gmail.readonly"),
                      signingIn(.gmail, .holding([googleOpen])))
            },
            Entry("signin-gmail-not-sent", size: CGSize(width: 480, height: 380)) {
                sheet(SignInSheet(kind: .gmail, startedHere: true, notSent: true),
                      signingIn(.gmail, .holding([googleOpen])))
            },
            Entry("signin-gmail-client-saved", size: tall) {
                sheet(SignInSheet(kind: .gmail), AppModel.preview(client: PreviewClient(
                    integrations: .success(clientSaved))))
            },
            Entry("signin-refused-link", size: tall) {
                sheet(SignInSheet(kind: .codex, startedHere: true), signingIn(.codex, .holding([
                    .open(url: "https://auth.openai.com.example.net/codex/device", code: "K7QF-2MXRA")])))
            },
            Entry("signin-google-workspace-waiting", size: short) {
                sheet(SignInSheet(kind: .googleWorkspace, startedHere: true),
                      signingIn(.googleWorkspace, .holding([googleOpen])))
            },
            Entry("signin-google-no-client", size: tall) {
                sheet(SignInSheet(kind: .googleWorkspace, startedHere: true),
                      signingIn(.googleWorkspace, .events([.error(StreamFailure(
                          "there is no Google OAuth client yet: download one from the Google Cloud console "
                              + "(Clients → Create client → Desktop app) and pass it with --client-file"))])))
            },
            Entry("signin-failed", size: short) {
                sheet(SignInSheet(kind: .claude, startedHere: true),
                      signingIn(.claude, .events([claudeOpen, .code,
                                                  .error("Claude did not accept that code; start the sign-in again.")])))
            },
            Entry("signin-cancelling", size: short) {
                sheet(SignInSheet(kind: .googleWorkspace, startedHere: true),
                      signingIn(.googleWorkspace, .slowToStop([googleOpen]), cancel: true))
            },
            Entry("signin-busy", size: CGSize(width: 480, height: 180)) {
                sheet(SignInSheet(kind: .claude), signingIn(.codex, .holding([codex])))
            },
        ]
    }

    private static let claudeOpen = StreamEvent<SignedIn>.open(
        url: "https://claude.com/cai/oauth/authorize?code=true&client_id=example&code_challenge=example&state=example",
        code: nil)
    private static let googleOpen = StreamEvent<SignedIn>.open(
        url: "https://accounts.google.com/o/oauth2/auth?response_type=code&state=example", code: nil)

    /// A sheet as it is presented: its model, and a browser that opens nothing.
    @MainActor private static func sheet(_ view: SignInSheet, _ model: AppModel) -> some View {
        view
            .environment(model)
            .environment(\.openURL, OpenURLAction { _ in .handled })
            .frame(maxHeight: .infinity, alignment: .top)
    }

    /// A model whose sign-in to `kind` follows `script`, left where the script leaves it
    /// (and asked to stop, with `cancel`).
    @MainActor private static func signingIn(_ kind: SignInProvider, _ script: PreviewClient.Script<SignedIn>,
                                             cancel: Bool = false) -> AppModel {
        let model = AppModel.preview(client: PreviewClient(signIn: script))
        model.signIn(kind)
        // The events land on the next turns of the main actor.
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.1))
        if cancel { model.signInRun.cancel() }
        return model
    }

    /// The preview's settings with `values` stored.
    private static func storing(_ values: [String: JSONValue]) -> [ConfigRow] {
        PreviewData.config.map { row in
            guard let value = values[row.key] else { return row }
            var row = row
            row.value = value
            row.set = true
            return row
        }
    }

    /// Every account connected.
    private static var allConnected: IntegrationsReport {
        var report = PreviewData.integrations
        report.integrations = report.integrations.map { integration in
            var integration = integration
            integration.connected = true
            integration.why = nil
            integration.secrets = integration.secrets.map { secret in
                var secret = secret
                secret.set = true
                return secret
            }
            return integration
        }
        return report
    }

    /// The Google client saved, and neither Google card signed in yet.
    private static var clientSaved: IntegrationsReport {
        var report = PreviewData.integrations
        for index in report.integrations.indices where report.integrations[index].clientSaved != nil {
            report.integrations[index].clientSaved = true
            if report.integrations[index].id == "gmail" { report.integrations[index].why = "Not signed in." }
        }
        return report
    }

    /// Google for agents on and not signed in; issue reports on, and gh not signed in.
    private static var halfConnected: IntegrationsReport {
        var report = PreviewData.integrations
        report.integrations = report.integrations.map { integration in
            var integration = integration
            switch integration.id {
            case "google": integration.why = "Not signed in."
            case "github": integration.why = "gh is not signed in to GitHub."
            default: break
            }
            return integration
        }
        return report
    }

    /// The whole window, on `item`.
    @MainActor private static func windowModel(on item: SidebarItem) -> AppModel {
        let model = AppModel.preview()
        model.selection = item
        return model
    }
}
