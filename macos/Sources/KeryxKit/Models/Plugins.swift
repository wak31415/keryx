import Foundation

/// `keryx plugins list --json`: the four plugins, each on or off, its settings as typed
/// fields, the accounts it needs, and whether it would work once on.
public struct PluginsReport: Codable, Sendable, Equatable {
    /// `DATA_DIR/tools`, where the plugins and the owner's own tools live.
    public var directory: String
    public var plugins: [Plugin]

    public func plugin(_ name: String) -> Plugin? { plugins.first { $0.name == name } }
}

/// One plugin: `send_to_slack`, `check_email`, `check_billing`, `cluster_stats`.
public struct Plugin: Codable, Sendable, Equatable, Identifiable {
    public var name: String
    /// Installed and loading: offered on the next call.
    public var on: Bool
    /// Its file is in the tools folder (it may still be refused).
    public var installed: Bool
    public var toolFile: String
    public var configFile: String
    /// Why an installed plugin does not load.
    public var refused: String?
    /// Its settings file as it stands; `nil` with none yet.
    public var settings: [String: JSONValue]?
    public var title: String
    public var description: String
    public var fields: [PluginField]
    /// The integrations it needs connected first (`Integration.id`s).
    public var needs: [String]
    /// It would work on a call once on.
    public var ready: Bool
    /// Why it would not, in a sentence ("Connect Slack in Integrations first.").
    public var why: String?

    public var id: String { name }
}

/// One of a plugin's settings, typed for a form.
public struct PluginField: Codable, Sendable, Equatable, Identifiable {
    public var key: String
    public var title: String
    /// `string`, `int`, `float`, `bool`, `choice` or `map`; see `kind`. Every number the
    /// CLI has is `float`, whole ones included.
    public var type: String
    /// A `choice`'s values.
    public var choices: [String]?
    /// Asked first; the rest are advanced.
    public var important: Bool
    /// What it is now (its default when the file does not say).
    public var value: JSONValue

    public var id: String { key }

    public var kind: PluginFieldType? { PluginFieldType(rawValue: type) }
}

public enum PluginFieldType: String, Sendable, CaseIterable {
    case string, int, float, bool, choice, map
}

/// `keryx plugins install|remove|set NAME --json`.
public struct PluginResult: Codable, Sendable, Equatable {
    public var name: String
    public var on: Bool
    /// What changed, in a sentence, said as it is.
    public var message: String

    public init(name: String, on: Bool, message: String) {
        self.name = name
        self.on = on
        self.message = message
    }
}

/// What `plugins install` or `plugins set` writes into a plugin's settings file. Never a
/// secret: those go through `config set KEY --stdin` or a sign-in.
public struct PluginValues: Sendable, Equatable {
    /// `--set KEY=VALUE` (install) or `KEY=VALUE` (set), one per setting.
    public var settings: [String: String]
    /// `cluster_stats` only: `--cluster ALIAS=PARTITION`, the whole `[clusters]` table.
    public var clusters: [String: String]
    /// `cluster_stats` only, install only: `--guard PATH`.
    public var guardScript: String?

    public init(settings: [String: String] = [:], clusters: [String: String] = [:], guardScript: String? = nil) {
        self.settings = settings
        self.clusters = clusters
        self.guardScript = guardScript
    }

    public var isEmpty: Bool { settings.isEmpty && clusters.isEmpty && guardScript == nil }
}

/// `keryx tools --json`: the owner's own voice tools in `DATA_DIR/tools` (the plugins
/// included), and the files the next call would refuse. The CLI exits 1 when any is
/// refused, with the document still printed.
public struct ToolsReport: Codable, Sendable, Equatable {
    public var directory: String
    public var tools: [CustomTool]
    /// Not in the contract by path (usually empty): `{file, reason}`.
    public var refused: [RefusedTool]
}

public struct CustomTool: Codable, Sendable, Equatable, Identifiable {
    public var name: String
    public var needsPin: Bool
    public var file: String
    public var description: String

    public var id: String { name }
}

public struct RefusedTool: Codable, Sendable, Equatable, Identifiable {
    public var file: String
    public var reason: String

    public var id: String { file }
}

/// `keryx plugins hosts --json`: the hosts in `~/.ssh/config` that `cluster_stats` could
/// ask. Only one with a ControlMaster is ever offered.
public struct SshHostsReport: Codable, Sendable, Equatable {
    public var hosts: [SshHost]
}

public struct SshHost: Codable, Sendable, Equatable, Identifiable {
    public var alias: String
    public var hostname: String
    public var user: String
    public var controlMaster: Bool
    public var controlPath: String
    /// Its master connection is up now (`ssh -O check`).
    public var masterAlive: Bool

    public var id: String { alias }
}
