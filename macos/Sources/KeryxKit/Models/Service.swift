import Foundation

/// `keryx service --json`: each of Keryx's units, installed and running or not.
public struct ServiceReport: Codable, Sendable, Equatable {
    /// `launchd` or `systemd`; `nil` on a machine with neither.
    public var manager: String?
    public var units: ServiceUnits
}

/// Keryx's four units.
public struct ServiceUnits: Codable, Sendable, Equatable {
    /// Keryx itself.
    public var agent: UnitState
    /// The tunnel in front of it.
    public var tunnel: UnitState
    /// The local model server.
    public var llm: UnitState
    /// The local voice server.
    public var voice: UnitState
}

public struct UnitState: Codable, Sendable, Equatable {
    public var installed: Bool
    public var active: Bool
}

/// `keryx service install|uninstall --json` and `keryx models stop --json`. Printed only
/// when it worked (`ok` is true); a failure is the command's exit and its stderr.
public struct ServiceResult: Codable, Sendable, Equatable {
    public var ok: Bool
    /// What the installer did ("installed dev.keryx.agent, dev.keryx.tunnel").
    public var message: String

    public init(ok: Bool, message: String) {
        self.ok = ok
        self.message = message
    }
}
