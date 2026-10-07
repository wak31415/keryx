import Foundation

/// `keryx integrations --json`: the accounts Keryx connects to, and whether each is.
public struct IntegrationsReport: Codable, Sendable, Equatable {
    public var integrations: [Integration]

    public func integration(_ id: String) -> Integration? { integrations.first { $0.id == id } }
}

/// One account: `google`, `gmail`, `slack`, `github`, `openai-admin`, `anthropic-admin`.
public struct Integration: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var title: String
    public var description: String
    public var connected: Bool
    /// Why it is not connected (or what being off means), in a sentence.
    public var why: String?
    /// The secrets it needs, each set or not — never a value. Not in the contract by path
    /// (several cards have none): `{key, title, set}`.
    public var secrets: [IntegrationSecret]
    /// The plain settings that turn it on (unset to disconnect).
    public var settings: [String]
    /// How it is connected when not by a secret: `gmail` or `google-workspace` (a sign-in,
    /// see `SignInProvider`), `gh` (GitHub's own CLI); `nil` for a secret alone.
    public var signin: String?
    /// The plugins that read it.
    public var usedBy: [String]

    /// The sign-in that connects it, when the app can run one.
    public var signInProvider: SignInProvider? { signin.flatMap(SignInProvider.init(rawValue:)) }
}

public struct IntegrationSecret: Codable, Sendable, Equatable, Identifiable {
    public var key: String
    public var title: String
    public var set: Bool

    public var id: String { key }
}

/// What `keryx auth login NAME --json` signs in to.
public enum SignInProvider: String, Sendable, CaseIterable, Identifiable {
    case claude, codex, gmail
    case googleWorkspace = "google-workspace"

    public var id: String { rawValue }

    /// The two that `keryx auth logout` can sign out of.
    public var canSignOut: Bool { self == .gmail || self == .googleWorkspace }

    /// It reads one line on stdin while it runs: Claude's code, Gmail's landing address.
    /// Codex's and Google for agents' read none.
    public var readsALine: Bool { self == .claude || self == .gmail }
}

/// `keryx auth logout gmail|google-workspace --json`. `signedIn` is always false: a sign-in
/// that was not there is said in `message`, not refused.
public struct SignOutResult: Codable, Sendable, Equatable {
    public var name: String
    public var signedIn: Bool
    public var message: String

    public init(name: String, signedIn: Bool, message: String) {
        self.name = name
        self.signedIn = signedIn
        self.message = message
    }
}
