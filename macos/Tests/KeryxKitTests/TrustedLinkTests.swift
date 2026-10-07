import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview

/// `TrustedLink`: the only addresses a sign-in may have the app open.
@Suite struct TrustedLinkTests {
    @Test(arguments: [
        "https://claude.ai/oauth/authorize?code=true",
        "https://claude.com/cai/oauth/authorize?code=true&state=example",
        "https://console.anthropic.com/settings/keys",
        "https://auth.openai.com/codex/device",
        "https://chatgpt.com/",
        "https://accounts.google.com/o/oauth2/auth?client_id=cid",
        "https://github.com/login/device",
        "HTTPS://Accounts.Google.com/o/oauth2/auth",
        "  https://auth.openai.com/codex/device\n",
    ])
    func aVendorsSignInPageOpens(_ address: String) throws {
        let url = try TrustedLink.url(address)
        #expect(url.scheme?.lowercased() == "https")
        #expect(TrustedLink.isTrusted(address))
    }

    @Test(arguments: [
        "http://accounts.google.com/o/oauth2/auth",            // not https
        "https://accounts.google.com.evil.example/auth",       // a lookalike
        "https://evil.example/?next=https://claude.ai",         // the host is what counts
        "https://login.github.com/",                            // no subdomains
        "https://user:pass@claude.ai/",                         // credentials
        "https://claude.ai:8443/",                              // a port
        "file:///Users/you/.config/keryx/secrets.toml",
        "javascript:alert(1)",
        "x-apple.systempreferences:com.apple.preference.security",
        "claude.ai/oauth",                                      // no scheme
        "",
    ])
    func anythingElseIsRefused(_ address: String) {
        #expect(throws: KeryxError.refused("Keryx won't open that address.")) {
            _ = try TrustedLink.url(address)
        }
        #expect(!TrustedLink.isTrusted(address))
    }

    @Test func everySignInFixturesPageIsTrusted() {
        for provider in SignInProvider.allCases {
            let links = PreviewData.signInLines(provider).compactMap { line -> String? in
                if case let .open(url, _) = SignInEvent.parse(line) { url } else { nil }
            }
            #expect(!links.isEmpty, "\(provider)")
            #expect(links.allSatisfy(TrustedLink.isTrusted), "\(provider)")
        }
    }

    @Test func theRefusalIsASentence() {
        let error = KeryxError.refused(TrustedLink.refusal)
        #expect(error.message == "Keryx won't open that address.")
        #expect(error.title == "Can't Do That")
    }
}
