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

    public var id: String { section + "/" + name }
}
