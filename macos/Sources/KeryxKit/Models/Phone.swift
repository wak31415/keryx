import Foundation

/// `keryx phone numbers --json`: the Twilio account's numbers. A report: Twilio refusing (or
/// nothing saved to ask it with) is `error`, with no numbers.
public struct PhoneNumbers: Codable, Sendable, Equatable {
    /// The account's name, once Twilio has answered.
    public var account: String?
    public var numbers: [PhoneNumber]
    public var error: String?
}

public struct PhoneNumber: Codable, Sendable, Equatable, Identifiable {
    /// E.164, as Twilio has it.
    public var number: String
    public var friendlyName: String
    /// Where its calls go now.
    public var voiceUrl: String?
    /// Both its webhooks point at this machine's `PUBLIC_HOST`.
    public var pointsHere: Bool

    public var id: String { number }
}

/// `keryx phone webhooks --json`: where `TWILIO_NUMBER`'s calls go now, and where they
/// should (`https://PUBLIC_HOST/…`). A report: what is missing is `error`.
public struct PhoneWebhooks: Codable, Sendable, Equatable {
    public var number: String?
    public var expected: Webhooks
    /// What Twilio says; `nil` until it has been asked successfully. Not in the contract by
    /// path, since it is so often null: its keys are `expected`'s.
    public var current: Webhooks?
    public var matches: Bool
    public var error: String?
}

/// A number's two webhooks.
public struct Webhooks: Codable, Sendable, Equatable {
    /// Where an incoming call is sent.
    public var voiceUrl: String?
    /// Where the call's status is sent.
    public var statusUrl: String?

    public init(voiceUrl: String?, statusUrl: String?) {
        self.voiceUrl = voiceUrl
        self.statusUrl = statusUrl
    }
}

/// `keryx phone webhooks --apply --json`: both webhooks pointed here — the one write Keryx
/// makes to Twilio, so the app asks before it.
public struct WebhooksApplied: Codable, Sendable, Equatable {
    public var applied: Bool
    public var number: String

    public init(applied: Bool, number: String) {
        self.applied = applied
        self.number = number
    }
}

/// `keryx phone tunnel --json`: the tunnel that puts this machine in front of Twilio.
public struct PhoneTunnel: Codable, Sendable, Equatable {
    /// `cloudflared` or `ngrok`, whichever is on PATH.
    public var kind: String?
    public var binary: String?
    public var publicHost: String?
    public var unitInstalled: Bool
    /// Keryx answers at `https://PUBLIC_HOST/health`; `nil` with no `PUBLIC_HOST`, false with
    /// `error` set for one that is not a bare hostname.
    public var reachable: Bool?
    public var error: String?
    /// Where to get cloudflared while no tunnel program is on PATH, else `nil`. A page the
    /// CLI names: opened only through `FixAction.tunnelDownload`'s check. Optional for an
    /// older CLI.
    public var installUrl: String?
    /// The public hostname the tunnel's own configuration already routes here
    /// (cloudflared's ingress, ngrok's reserved domain), to offer for `PUBLIC_HOST`. `nil`
    /// from a CLI without it, or when it names none.
    public var hostname: String?
}
