import Foundation

/// `keryx service --json`: each of Keryx's units, installed and running or not.
public struct ServiceReport: Codable, Sendable, Equatable {
    /// `launchd` or `systemd`; `nil` on a machine with neither.
    public var manager: String?
    public var units: ServiceUnits
}

/// Keryx's units.
public struct ServiceUnits: Codable, Sendable, Equatable {
    /// Keryx itself.
    public var agent: UnitState
    /// The tunnel in front of it.
    public var tunnel: UnitState
    /// The local model server.
    public var llm: UnitState
    /// The local voice server.
    public var voice: UnitState
    /// The voice LLM's own model server (`dev.keryx.voice-llm`); `nil` from a CLI older
    /// than it.
    public var voiceLlm: UnitState?
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

/// `keryx service stop|start [--all] --json`: unlike `install`/`uninstall`, this document is
/// printed on every path, including a refusal — `units` is `[]` then. Keeps the unit file
/// (and so macOS's install), only loaded or not: `stop` disables and unloads, `start`
/// enables and starts (or re-kickstarts) what is already there, installing nothing.
public struct ServiceControlResult: Codable, Sendable, Equatable {
    public var ok: Bool
    /// For a person: no commands, no paths.
    public var message: String
    /// The manager's own last line for each unit that failed, redacted; `nil` otherwise.
    public var detail: String?
    public var units: [ServiceControlUnit]

    public init(ok: Bool, message: String, detail: String? = nil, units: [ServiceControlUnit] = []) {
        self.ok = ok
        self.message = message
        self.detail = detail
        self.units = units
    }
}

/// One of `ServiceControlResult.units`, in the order the CLI acted on them.
public struct ServiceControlUnit: Codable, Sendable, Equatable {
    /// `agent`, `tunnel`, `llm`, `voice`, … — more than `ServiceUnits` has fields for.
    public var kind: String
    public var unit: String
    /// `running`, `stopped`, `not-installed` or `failed`.
    public var state: String

    public init(kind: String, unit: String, state: String) {
        self.kind = kind
        self.unit = unit
        self.state = state
    }
}
