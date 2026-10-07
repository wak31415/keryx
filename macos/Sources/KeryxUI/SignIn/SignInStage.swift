import Foundation
import KeryxKit

/// Where a sign-in sheet is: what it shows and which buttons it offers, read from the one
/// `signInRun` every sign-in shares.
enum SignInStage: Equatable {
    /// Not started: what will happen, and Sign In.
    case ready
    /// Another sign-in holds `signInRun`: this one waits for it.
    case busyElsewhere(SignInProvider?)
    /// Started, with nothing to open yet.
    case starting(String)
    /// The page is open, and the sign-in waits for the browser (Claude before its code,
    /// Google for agents until it is approved).
    case browser(StreamRun<SignedIn>.Link)
    /// Codex's page is open, with its code to type there.
    case deviceCode(StreamRun<SignedIn>.Link)
    /// Claude showed a code, and the CLI waits for it.
    case wantsCode(StreamRun<SignedIn>.Link)
    /// Google landed the browser on an address, which the CLI waits for.
    case wantsAddress(StreamRun<SignedIn>.Link)
    /// The code or the address went in; the CLI is checking it.
    case checking
    case cancelling
    case failed(StreamFailure)
    case finished(SignedIn)

    /// `kind`'s stage. `startedHere`: this sheet started the run `signInRun` holds;
    /// `signingIn`: whose run that is. `sent`: this sheet has handed the CLI its line.
    static func of(_ kind: SignInProvider, phase: StreamRun<SignedIn>.Phase?, signingIn: SignInProvider?,
                   startedHere: Bool, sent: Bool) -> SignInStage {
        let busy = phase == .cancelling || { if case .running = phase { true } else { false } }()
        let ours = signingIn == kind && (startedHere || busy)
        guard ours else { return busy ? .busyElsewhere(signingIn) : .ready }
        switch phase {
        case nil:
            return .ready
        case .cancelling:
            return .cancelling
        case let .failed(failure):
            return .failed(failure)
        case let .finished(done):
            return .finished(done)
        case let .running(activity):
            guard let link = activity.link else { return .starting(activity.message) }
            switch kind {
            case .claude:
                if activity.wantsCode { return .wantsCode(link) }
                return sent ? .checking : .browser(link)
            case .gmail:
                return sent ? .checking : .wantsAddress(link)
            case .codex:
                return .deviceCode(link)
            case .googleWorkspace:
                return .browser(link)
            }
        }
    }

    /// The page the browser should show now; `nil` when there is none.
    var link: StreamRun<SignedIn>.Link? {
        switch self {
        case let .browser(link), let .deviceCode(link), let .wantsCode(link), let .wantsAddress(link): link
        default: nil
        }
    }

    /// A run is going in it (Cancel stops the run rather than closing the sheet).
    var isRunning: Bool {
        switch self {
        case .starting, .browser, .deviceCode, .wantsCode, .wantsAddress, .checking: true
        default: false
        }
    }
}

/// The sign-in sheets' words.
public enum SignInWording {
    /// What the sign-in is called in a sentence.
    public static func name(_ provider: SignInProvider) -> String {
        switch provider {
        case .claude: "Claude"
        case .codex: "Codex"
        case .gmail: "Gmail"
        case .googleWorkspace: "Google for agents"
        }
    }

    /// The sheet's title.
    static func title(_ provider: SignInProvider) -> String {
        switch provider {
        case .googleWorkspace: "Sign In to Google for Agents"
        default: "Sign In to \(name(provider))"
        }
    }

    /// What signing in does, and what the owner will be asked to do.
    static func intro(_ provider: SignInProvider) -> String {
        switch provider {
        case .claude:
            "Claude's tasks run on your Claude subscription. Your browser opens Claude's sign-in page; once you "
                + "approve, it shows a code to paste here."
        case .codex:
            "Codex's tasks run on your ChatGPT plan. Your browser opens OpenAI's sign-in page, where you type the "
                + "code shown here."
        case .gmail:
            "Email answers gets a read-only look at your mailbox. Your browser opens Google's consent page; when "
                + "it's done, it lands on a page that doesn't load. Copy that page's address and paste it here."
        case .googleWorkspace:
            "Every agent's tasks get Gmail and Calendar through Google's workspace server. Your browser opens "
                + "Google's consent page, and the sign-in finishes on its own once you approve."
        }
    }

    /// The line under the spinner while the run waits.
    static func waiting(_ stage: SignInStage, provider: SignInProvider) -> String? {
        switch stage {
        case let .starting(message): message
        case .browser:
            provider == .claude
                ? "Approve the sign-in in your browser. Claude then shows a code."
                : "Waiting for you to approve the sign-in in your browser…"
        case .deviceCode: "Waiting for you to finish in your browser…"
        case .checking: provider == .claude ? "Checking the code…" : "Finishing the sign-in…"
        case .cancelling: "Stopping…"
        default: nil
        }
    }

    static let codePrompt = "Paste the code Claude showed"
    static let addressPrompt = "Paste the address your browser landed on"
    static let deviceCodeLead = "Type this code on the page that opened:"

    /// Why it failed, as the sheet says it: a missing Google client in the app's words,
    /// anything else as the CLI said it.
    static func failure(_ message: String) -> String {
        if GoogleClient.isMissing(message) {
            return "There's no Google OAuth client yet. Choose the client file you downloaded, then try again."
        }
        return Wording.sentence(Wording.capitalised(message))
    }

    /// Said under a finished sign-in: a token kept as a setting is loaded by a restart.
    static func finished(_ done: SignedIn) -> String? {
        done.stored == nil ? nil : "Keryx starts using it after its next restart."
    }

    static func busyElsewhere(_ other: SignInProvider?) -> String {
        let what = other.map { "A sign-in to \(name($0))" } ?? "Another sign-in"
        return "\(what) is still going. Finish or cancel it first."
    }

    /// The page to open for `link`: only one `TrustedLink` allows (https, on a vendor's own
    /// sign-in host), never anything else a CLI might print.
    static func page(_ link: StreamRun<SignedIn>.Link?) -> URL? {
        link.flatMap { try? TrustedLink.url($0.url) }
    }

    /// The address Google's consent page sends the browser to, as the one line Gmail's
    /// sign-in reads: http on this Mac (localhost, 127.0.0.1 or [::1]) with a `code` or an
    /// `error` in its query. `nil` for anything else — stdin closes after one line, so a
    /// wrong paste would end the sign-in.
    public static func landingAddress(_ typed: String) -> String? {
        let text = line(typed)
        guard !text.isEmpty, let parts = URLComponents(string: text), parts.scheme?.lowercased() == "http",
              let host = parts.host?.lowercased(), ["localhost", "127.0.0.1", "::1", "[::1]"].contains(host),
              parts.user == nil, parts.password == nil,
              parts.queryItems?.contains(where: { ($0.name == "code" || $0.name == "error") && $0.value != nil }) == true
        else { return nil }
        return text
    }

    /// Said under the address field while what is in it is not that address.
    static let notLandingAddress =
        "That isn't the address Google sent your browser to. It begins http://localhost and has code= in it."

    /// Said when the line could not be handed to the CLI (it had stopped reading).
    static let notSent = "The sign-in had stopped listening, so that didn't reach it. Cancel, and start it again."

    /// What quitting during the sign-in to `provider` loses.
    public static func quitDetail(_ provider: SignInProvider) -> String {
        switch provider {
        case .googleWorkspace:
            "The sign-in stops. Google for agents stays turned on without a sign-in until you sign in again or "
                + "disconnect it."
        case .claude, .codex, .gmail:
            "The sign-in stops, and nothing is saved. You can start it again later."
        }
    }

    /// What was pasted, as the one line the CLI reads: trimmed, and empty when it holds a
    /// line break inside (which the CLI could not take as one line).
    static func line(_ typed: String) -> String {
        let trimmed = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.contains(where: \.isNewline) ? "" : trimmed
    }
}
