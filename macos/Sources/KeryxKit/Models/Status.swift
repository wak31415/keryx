import Foundation

/// `keryx status --json`: one document for the menu bar, every part reporting itself
/// (`ok`/`error`) so one broken probe never hides the rest. See Task 3 in the plan for the
/// exact shape the CLI prints.
public struct StatusReport: Codable, Sendable, Equatable {
    public var schema: Int
    public var storage: Storage
    public var service: Service
    public var health: Health
    public var version: Version
    public var restart: Restart
    public var assistant: Assistant
    public var agents: Agents
    public var tasks: TaskCounts
    public var approvals: ApprovalsState
    public var paths: Paths
}

public struct Storage: Codable, Sendable, Equatable {
    public var ok: Bool
    public var error: String?
    public var migrationRequired: Bool
}

public struct Service: Codable, Sendable, Equatable {
    public var ok: Bool
    public var error: String?
    public var manager: String?
    public var unit: String?
    public var installed: Bool
    public var active: Bool
}

public struct Health: Codable, Sendable, Equatable {
    public var ok: Bool
    public var error: String?
    public var reachable: Bool
    public var liveSessions: Int?
}

public struct Version: Codable, Sendable, Equatable {
    public var ok: Bool
    public var error: String?
    public var running: String?
    public var checkout: String?
}

public struct Restart: Codable, Sendable, Equatable {
    public var ok: Bool
    public var error: String?
    public var state: String?
    public var requestedAt: String?
    public var failure: String?
}

public struct Assistant: Codable, Sendable, Equatable {
    public var ok: Bool
    public var error: String?
    public var name: String
}

public struct Agents: Codable, Sendable, Equatable {
    public var ok: Bool
    public var error: String?
    public var `default`: String
    public var enabled: [String]
}

public struct TaskCounts: Codable, Sendable, Equatable {
    public var ok: Bool
    public var error: String?
    public var running: Int
    public var queued: Int
    public var unreported: Int
}

public struct ApprovalsState: Codable, Sendable, Equatable {
    public var ok: Bool
    public var error: String?
    public var escalationEnabled: Bool
}

public struct Paths: Codable, Sendable, Equatable {
    public var ok: Bool
    public var error: String?
    public var config: String
    public var data: String
    public var state: String
    public var cache: String
    public var logs: String
    public var memory: String
}
