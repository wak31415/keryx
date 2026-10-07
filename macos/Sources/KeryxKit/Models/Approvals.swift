import Foundation

/// `keryx approvals --json`: the kill switch, the escalation settings, and the audit trail.
public struct ApprovalsReport: Codable, Sendable, Equatable {
    public var escalationEnabled: Bool
    public var escalateAfterS: Double
    public var maxPerHour: Int
    public var cancelledBeforeCall: Int
    public var answeredAfterCall: Int
    public var entries: [ApprovalEntry]
}

/// One line of the approval audit log. `requestId` is whatever the hook sent — an int most
/// of the time, occasionally a string — so it decodes as `JSONValue`, never assumed numeric.
public struct ApprovalEntry: Codable, Sendable, Equatable, Identifiable {
    public var ts: String
    public var event: String
    public var what: String
    public var requestId: JSONValue?

    public var id: String { ts + event + what }
}
