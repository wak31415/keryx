import Foundation
import KeryxKit

/// How the Phone section says the Twilio account, the number, its webhooks and the tunnel,
/// from `keryx phone numbers|webhooks|tunnel --json`.
enum PhoneWording {
    /// A line with a dot: a tone, the words, and what more there is to say under them.
    struct Line: Equatable {
        let tone: StatusDot.Tone
        let text: String
        var hint: String?
    }

    // MARK: - The account

    /// The keys the Phone section shows as settings rows, and whose change is read again.
    static let accountKeys = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"]
    static let callerKeys = ["ALLOWED_CALLERS", "OWNER_NUMBER", "KERYX_PIN"]

    /// The account: connected, with its name under the row's title once Twilio has
    /// answered, else why not. `titles`: the
    /// settings' titles by key, to say a key the CLI names in.
    static func account(_ numbers: PhoneNumbers?, sidSet: Bool, tokenSet: Bool,
                        titles: [String: String] = [:]) -> Line {
        if let name = numbers?.account, !name.isEmpty {
            return Line(tone: .ok, text: "Connected", hint: name)
        }
        guard sidSet, tokenSet else {
            return Line(tone: .inactive, text: "Not connected",
                        hint: "Your account SID and auth token are on Twilio's console")
        }
        if let error = numbers?.error, !error.isEmpty {
            return Line(tone: .error, text: "Not connected", hint: said(error, titles: titles))
        }
        return Line(tone: .inactive, text: numbers == nil ? "Checking…" : "Connected")
    }

    /// An account SID as Twilio's console shows it: `AC` and 32 hexadecimal digits.
    static func sidProblem(_ sid: String) -> String? {
        let trimmed = sid.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        let hex = trimmed.dropFirst(2)
        if trimmed.hasPrefix("AC"), hex.count == 32, hex.allSatisfy(\.isHexDigit) { return nil }
        return "An account SID is AC and 32 more letters and digits, as Twilio's console shows it."
    }

    /// What the account sheet says of the check (`config verify TWILIO_AUTH_TOKEN --pair`).
    static func accountCheck(_ check: SecretCheck) -> (text: String, works: Bool)? {
        switch check {
        case .unchecked, .checking:
            return nil
        case let .works(detail):
            guard let detail, !detail.isEmpty else { return ("Twilio accepted them.", true) }
            return ("Twilio accepted them: \(detail).", true)
        case let .problem(sentence):
            return (sentence, false)
        }
    }

    /// The CLI's sentence, its keys said by their titles ("There's no “Twilio number” saved.").
    static func said(_ text: String, titles: [String: String]) -> String {
        KeryxError.sentence(Wording.named(text, titles: titles))
    }

    /// The line under the number picker.
    static func numberCaption(_ numbers: Loadable<PhoneNumbers>, connected: Bool, titles: [String: String]) -> String {
        if !connected { return "Connect your Twilio account first, then choose its number." }
        if let report = numbers.value {
            // The account row says why already; this one says what follows.
            if report.error != nil, report.numbers.isEmpty { return "Its numbers are listed here once Twilio answers." }
            if report.numbers.isEmpty { return "This account has no numbers yet. Buy one on Twilio's console." }
        } else if let error = numbers.error {
            return error.message
        }
        return "The number Keryx answers and calls from."
    }

    /// A write in the account sheet that failed: the app's sentence, and what Keryx said
    /// under Show Details.
    static func saveFailure(_ error: KeryxError) -> StreamFailure {
        error.said("The Twilio account wasn't saved.")
    }

    /// The new account SID was saved and the token was not: Keryx now holds the new SID
    /// with the old token, which Twilio will refuse.
    static func tokenNotSaved(_ error: KeryxError) -> StreamFailure {
        error.said("The new account SID was saved, but the auth token wasn't. "
            + "Keryx now has the new SID with the old token, which Twilio won't accept together. "
            + "Save again to put the token beside it, or change the SID back.")
    }

    // MARK: - The number

    /// One number the picker offers.
    struct NumberChoice: Equatable, Identifiable {
        let number: String
        /// "Office · +15550001111", or the number alone when its name is the number.
        let label: String
        /// Its webhooks point here already.
        let pointsHere: Bool

        var id: String { number }
    }

    /// The choices: the account's numbers, and the saved one too when the account does not
    /// list it (another account's, or a typing mistake), so the picker can still show it.
    static func choices(_ numbers: [PhoneNumber], saved: String?) -> [NumberChoice] {
        let listed = numbers.map { NumberChoice(number: $0.number, label: label($0), pointsHere: $0.pointsHere) }
        guard let saved, !saved.isEmpty, !numbers.contains(where: { $0.number == saved }) else { return listed }
        return [NumberChoice(number: saved, label: "\(saved) (not in this account)", pointsHere: false)] + listed
    }

    static func label(_ number: PhoneNumber) -> String {
        let name = number.friendlyName.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty, name != number.number else { return number.number }
        return "\(name) · \(number.number)"
    }

    // MARK: - The webhooks

    /// Whether `TWILIO_NUMBER`'s calls come here.
    static func webhooks(_ report: PhoneWebhooks, titles: [String: String] = [:]) -> Line {
        if let error = report.error, !error.isEmpty {
            return Line(tone: .inactive, text: "Not checked", hint: said(error, titles: titles))
        }
        let number = report.number ?? "The number"
        if report.matches {
            return Line(tone: .ok, text: "\(number)'s calls come to this Mac")
        }
        if report.current == nil {
            return Line(tone: .warning, text: "Couldn't ask Twilio where \(number)'s calls go",
                        hint: "Point Here is offered once Twilio answers")
        }
        return Line(tone: .warning, text: "\(number)'s calls go elsewhere",
                    hint: "Point Here sends them to this Mac")
    }

    /// Point Here can be offered: Twilio has said where the number points now (so the
    /// question can show both addresses), there is somewhere to point it, and it does not
    /// point there already.
    static func canApply(_ report: PhoneWebhooks) -> Bool {
        report.error == nil && report.current != nil && report.number != nil
            && report.expected.voiceUrl != nil && report.expected.statusUrl != nil && !report.matches
    }

    /// Point Here may be clicked: Twilio answered the read this question made (`fresh`), that
    /// read is not still going nor failed, and nothing is being applied — so the yes covers
    /// exactly the addresses shown.
    static func canPointHere(fresh: PhoneWebhooks?, asking: Bool, readFailed: Bool, applying: Bool) -> Bool {
        guard let fresh, !asking, !readFailed, !applying else { return false }
        return canApply(fresh)
    }

    /// One webhook, now and as it should be.
    struct Hook: Equatable, Identifiable {
        let title: String
        let now: String?
        let expected: String?

        var id: String { title }

        /// Trailing slashes aside, as Keryx compares them.
        var matches: Bool {
            guard let now, let expected else { return false }
            return Self.trimmed(now) == Self.trimmed(expected)
        }

        private static func trimmed(_ url: String) -> String {
            url.hasSuffix("/") ? String(url.dropLast()) : url
        }
    }

    static func hooks(_ report: PhoneWebhooks) -> [Hook] {
        [Hook(title: "Incoming calls", now: report.current?.voiceUrl, expected: report.expected.voiceUrl),
         Hook(title: "Call status", now: report.current?.statusUrl, expected: report.expected.statusUrl)]
    }

    /// The question before the one write Keryx makes to Twilio.
    static func applyTitle(_ report: PhoneWebhooks) -> String {
        "Point \(report.number ?? "the Number") at This Mac?"
    }

    static let applyMessage = "Twilio will send this number's calls, and their status, to the addresses below. "
        + "This changes the number in your Twilio account; it's the one change Keryx makes there."

    // MARK: - The tunnel

    /// Whether a tunnel program is installed; which one, under the row's title.
    static func tunnel(_ tunnel: PhoneTunnel) -> Line {
        guard let kind = tunnel.kind else {
            return Line(tone: .warning, text: "Not installed",
                        hint: "cloudflared or ngrok puts this Mac where Twilio can reach it")
        }
        return Line(tone: .ok, text: "Installed", hint: kind)
    }

    /// Whether the tunnel runs as a service (installed with Keryx's own, in the Overview).
    static func tunnelService(_ tunnel: PhoneTunnel) -> Line {
        tunnel.unitInstalled
            ? Line(tone: .ok, text: "Installed")
            : Line(tone: .inactive, text: "Not installed", hint: "Installing Keryx's service in the Overview installs it too")
    }

    /// Whether Twilio can reach Keryx at `https://PUBLIC_HOST`.
    static func reachable(_ tunnel: PhoneTunnel) -> Line {
        switch tunnel.reachable {
        case nil:
            return Line(tone: .inactive, text: "No public hostname yet")
        case true?:
            return Line(tone: .ok, text: "Keryx answers at \(tunnel.publicHost ?? "its hostname")")
        case false?:
            let hint = tunnel.error.map(KeryxError.sentence)
                ?? "Nothing answered at https://\(tunnel.publicHost ?? "")/health. Is Keryx running, and its tunnel?"
            return Line(tone: .error, text: "Not reachable", hint: hint)
        }
    }
}
