import KeryxKit
import SwiftUI

/// How a card on the Integrations screen connects its account.
enum IntegrationConnect: Equatable {
    /// A secret typed into `SecretSheet` (`config set KEY --stdin`).
    case secret(IntegrationSecret)
    /// A sign-in sheet (`auth login NAME --json`).
    case signIn(SignInProvider)
    /// GitHub: `ISSUE_REPORTING` turned on here, and `gh` installed and signed in by hand.
    case issueReports
}

/// What Integrations does for a request from another window.
enum IntegrationsAnswer: Equatable {
    /// Opens this sheet.
    case open(IntegrationSheet)
    /// Does what the card's own Connect does.
    case connect(Integration)
    /// The cards are not read yet: answered once they are.
    case wait
    /// No card has that id: the section is shown, and nothing more.
    case unknown
}

/// What Disconnect does: the settings `config unset` removes, then the sign-in
/// `auth logout` removes.
struct IntegrationDisconnect: Equatable {
    var unset: [String]
    var signOut: SignInProvider?
}

/// A question asked before a change: its title, its sentence, and the button that agrees.
struct IntegrationQuestion: Equatable {
    var title: String
    var message: String
    var button: String
}

/// The Integrations screen's words and decisions, kept apart from the views so they can be
/// tested.
enum IntegrationsWording {
    static let issueReportsKey = "ISSUE_REPORTING"
    static let issueRepoKey = "ISSUE_REPO"

    // MARK: Connecting

    /// How `integration` connects; `nil` for one with nothing the app can do.
    static func connect(_ integration: Integration) -> IntegrationConnect? {
        if let provider = integration.signInProvider { return .signIn(provider) }
        if integration.signin == "gh" { return .issueReports }
        if let secret = integration.secrets.first(where: { !$0.set }) ?? integration.secrets.first {
            return .secret(secret)
        }
        return nil
    }

    /// Everything Disconnect removes: its secrets and settings, and for the two Google
    /// sign-ins their stored tokens. GitHub's Turn Off unsets only `ISSUE_REPORTING`, which is
    /// what turns reports off, and keeps a repository of the owner's choosing.
    static func disconnect(_ integration: Integration) -> IntegrationDisconnect {
        if integration.signin == "gh" { return IntegrationDisconnect(unset: [issueReportsKey], signOut: nil) }
        return IntegrationDisconnect(unset: integration.secrets.map(\.key) + integration.settings,
                                     signOut: integration.signInProvider.flatMap { $0.canSignOut ? $0 : nil })
    }

    /// Something of it is stored for Disconnect to remove: it is connected, one of its
    /// secrets is set, or (once the settings are read) one of its settings is.
    static func canDisconnect(_ integration: Integration, config: [ConfigRow]?) -> Bool {
        if integration.connected || integration.secrets.contains(where: \.set) { return true }
        guard let config else { return false }
        return integration.settings.contains { key in config.first { $0.key == key }?.set == true }
    }

    /// Issue reports are on (`ISSUE_REPORTING`); `nil` until the settings are read.
    static func issueReportsOn(_ config: [ConfigRow]?) -> Bool? {
        guard let row = config?.first(where: { $0.key == issueReportsKey }) else { return nil }
        if case let .bool(on) = row.value { return on }
        return row.value?.displayText.lowercased() == "true"
    }

    /// The repository issues are filed on, as the settings say; `nil` until they are read.
    static func issueRepo(_ config: [ConfigRow]?) -> String? {
        guard let text = config?.first(where: { $0.key == issueRepoKey })?.value?.displayText,
              !text.isEmpty else { return nil }
        return text
    }

    // MARK: Requests

    /// What Integrations does for a request from another window (`RequestAnswer`): open a
    /// Google sign-in's sheet, or press a card's own Connect — `wait` while the cards are
    /// still being read (`cards` nil), `unknown` for a card there is none of. `nil` for a
    /// request that is not Integrations'.
    static func answer(_ request: AppRequest, cards: [Integration]?) -> IntegrationsAnswer? {
        guard case let .integrations(what) = RequestAnswer.of(request) else { return nil }
        switch what {
        case let .signIn(provider):
            return .open(.signIn(provider))
        case let .connect(id):
            guard let cards else { return .wait }
            return cards.first { $0.id == id }.map(IntegrationsAnswer.connect) ?? .unknown
        }
    }

    // MARK: Status

    static func status(_ integration: Integration) -> String {
        integration.connected ? "Connected" : "Not Connected"
    }

    /// Green when connected; orange when something of it is stored and it still does not
    /// work (a sign-in half done, reports on and `gh` missing); grey otherwise.
    static func tone(_ integration: Integration, config: [ConfigRow]?) -> StatusDot.Tone {
        if integration.connected { return .ok }
        return canDisconnect(integration, config: config) ? .warning : .inactive
    }

    /// "The Slack plugin", "The Email answers and Billing plugins"; `nil` with none.
    static func usedBy(_ names: [String], plugins: PluginsReport?) -> String? {
        let titles = names.map { plugins?.plugin($0)?.title ?? $0 }
        guard let last = titles.last else { return nil }
        if titles.count == 1 { return "The \(last) plugin" }
        return "The \(titles.dropLast().joined(separator: ", ")) and \(last) plugins"
    }

    // MARK: Questions

    /// Asked before Disconnect (or, for GitHub, Turn Off).
    static func disconnectQuestion(_ integration: Integration, plugins: PluginsReport?) -> IntegrationQuestion {
        if integration.signin == "gh" {
            return IntegrationQuestion(
                title: "Turn Off Issue Reports?",
                message: "Bugs and feature requests you report on a call are no longer filed on GitHub. "
                    + "gh stays signed in.",
                button: "Turn Off")
        }
        var sentences: [String] = []
        switch integration.signInProvider {
        case .googleWorkspace:
            sentences.append("Agents' tasks lose Gmail and Calendar through Google's workspace server, "
                + "and the stored sign-in is removed. Your Google OAuth client file stays.")
        case .gmail:
            sentences.append("The stored sign-in is removed. Your Google OAuth client file stays.")
        case .claude, .codex, nil:
            for secret in integration.secrets {
                sentences.append("The \(secret.title) is removed from secrets.toml.")
            }
        }
        if let stops = stopsWorking(integration.usedBy, plugins: plugins) { sentences.append(stops) }
        return IntegrationQuestion(title: "Disconnect \(integration.title)?",
                                   message: sentences.joined(separator: " "), button: "Disconnect")
    }

    /// "Email answers stops working until you connect it again."
    static func stopsWorking(_ names: [String], plugins: PluginsReport?) -> String? {
        let titles = names.map { plugins?.plugin($0)?.title ?? $0 }
        guard let last = titles.last else { return nil }
        if titles.count == 1 { return "The \(last) plugin stops working until you connect it again." }
        return "The \(titles.dropLast().joined(separator: ", ")) and \(last) plugins stop working until you "
            + "connect it again."
    }

    /// Asked before issue reports are turned on: they are published.
    static func issueReportsQuestion(repo: String?) -> IntegrationQuestion {
        let place = repo.map { "on \($0)" } ?? "on Keryx's own repository"
        return IntegrationQuestion(
            title: "Turn On Issue Reports?",
            message: "When you report a bug or ask for a feature on a call, a subagent files it with gh as an "
                + "issue \(place), which anyone can read. It files the pattern only: never your number, your PIN, "
                + "a secret, or anything said on the call.",
            button: "Turn On")
    }

    // MARK: Looks

    /// The symbol on the card's tile.
    static func symbol(_ id: String) -> String {
        switch id {
        case "google": "calendar"
        case "gmail": "envelope.fill"
        case "slack": "number"
        case "github": "ladybug.fill"
        case "openai-admin", "anthropic-admin": "key.fill"
        default: "link"
        }
    }

    /// The tile's colour: one per account, none a brand's.
    static func tint(_ id: String) -> Color {
        switch id {
        case "google": .blue
        case "gmail": .red
        case "slack": .purple
        case "github": .gray
        case "openai-admin": .teal
        case "anthropic-admin": .orange
        default: .gray
        }
    }
}

/// Google's sign-ins share one OAuth client file of the owner's own.
enum GoogleClient {
    /// Whether the client is saved: the Google cards' `client_saved`, or, from a CLI older
    /// than that field, `false` when a card's `why` asks for the file. `nil` when unknown —
    /// before the accounts are read, or when an older CLI does not say.
    static func saved(_ report: IntegrationsReport?) -> Bool? {
        let cards = ["gmail", "google"].compactMap { report?.integration($0) }
        if let saved = cards.lazy.compactMap(\.clientSaved).first { return saved }
        return cards.contains { $0.why?.contains("OAuth client file") == true } ? false : nil
    }

    /// No client is saved: the sign-in sheet asks for the downloaded file first. A sign-in
    /// that finds none when this could not tell says so, and the sheet asks then
    /// (`isMissing`).
    static func needsFile(_ report: IntegrationsReport?) -> Bool {
        saved(report) == false
    }

    /// A sign-in failed for want of a client file (`google.NO_CLIENT`).
    static func isMissing(_ failure: String) -> Bool {
        failure.contains("no Google OAuth client")
    }

    /// Where the client is made. Ours, not a CLI's output, so it is a plain `Link` and not
    /// passed through `TrustedLink`.
    static let console = URL(string: "https://console.cloud.google.com/auth/clients")!
}
