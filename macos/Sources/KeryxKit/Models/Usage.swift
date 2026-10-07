import Foundation

/// `keryx tasks usage --json`: what each project's tasks have spent, summed server-side.
public struct UsageReport: Codable, Sendable, Equatable {
    public var since: Date?
    public var projects: [ProjectUsage]
}

/// One row of the usage table. The CLI's dict also carries `project`, `internal` and
/// `measured` (see `ProjectUsage` in `src/keryx/tasks/models.py`) — ignored here along with
/// any other key Codable does not recognise, since the app reads only the label already
/// resolved for display.
public struct ProjectUsage: Codable, Sendable, Equatable, Identifiable {
    public var label: String
    public var tasks: Int
    public var priced: Int
    public var inputTokens: Int
    public var outputTokens: Int
    public var costUsd: Double?

    public var id: String { label }
}
