import Foundation

/// The addresses a sign-in may ask the app to open (its `open` event): https, on one of a
/// few vendors' own hosts, nothing else. The link comes from the CLI's stdout, and the app
/// opens it in the owner's browser, so a link elsewhere — a lookalike host, plain http, a
/// `file:` or custom scheme, credentials or a port in it — is refused rather than opened.
public enum TrustedLink {
    /// The sign-in pages' hosts, matched exactly (no subdomains): Claude's (`claude.com` is
    /// where `claude setup-token` sends the owner), Anthropic's console, OpenAI's, Google's
    /// and GitHub's.
    public static let hosts: Set<String> = [
        "claude.ai", "claude.com", "console.anthropic.com", "auth.openai.com", "chatgpt.com",
        "accounts.google.com", "github.com",
    ]

    /// What a refused address says.
    public static let refusal = "Keryx won't open that address."

    /// `address` as a URL to open, or `KeryxError.refused` when it is not one of these.
    public static func url(_ address: String) throws -> URL {
        let trimmed = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let parts = URLComponents(string: trimmed), parts.scheme?.lowercased() == "https",
              let host = parts.host?.lowercased(), hosts.contains(host),
              parts.user == nil, parts.password == nil, parts.port == nil,
              let url = parts.url
        else { throw KeryxError.refused(refusal) }
        return url
    }

    /// Whether `address` would be opened.
    public static func isTrusted(_ address: String) -> Bool {
        (try? url(address)) != nil
    }
}
