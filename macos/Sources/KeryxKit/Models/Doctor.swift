import Foundation

/// `keryx doctor --json`.
public struct DoctorReport: Codable, Sendable, Equatable {
    public var ok: Bool
    public var checks: [DoctorCheck]
}

/// One line of `keryx doctor`. `state` is derived on the Python side from `ok`/`unset`
/// (`"ok"` / `"missing"` / `"failed"`) and carried as a string here rather than re-derived,
/// so the two can never disagree.
public struct DoctorCheck: Codable, Sendable, Equatable, Identifiable {
    public var name: String
    public var detail: String
    public var severity: String
    public var section: String
    public var state: String
    public var ok: Bool
    public var unset: Bool
    /// What to call the check: a setting's display title when the check is named after one
    /// ("OpenAI API key" for `OPENAI_API_KEY`), else the name with a capital first letter.
    /// Optional, so a CLI from before it existed still decodes.
    public var title: String?
    /// The check could not ask what it checks (Twilio not answering): shown as failed, but
    /// holding nothing back — not a step's reason, and not `ready`'s. Optional, so a CLI
    /// from before it existed still decodes; see `couldNotAsk`.
    public var unknown: Bool?

    /// `unknown`, false when the CLI did not say.
    public var couldNotAsk: Bool { unknown ?? false }

    /// `title`, else the name as doctor gives it.
    public var displayTitle: String { title ?? name }

    public var id: String { section + "/" + name }
}
