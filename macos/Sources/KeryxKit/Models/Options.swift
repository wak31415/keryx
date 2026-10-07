import Foundation

/// `keryx config options KEY --json`: what a setting may be set to, asked of whoever knows
/// (OpenAI's or Anthropic's model list, the Codex app-server, the model server, the catalog
/// Keryx ships, or a list built in). A list that could not be asked live is still an answer:
/// `live` is false, `error` says why, and `options` is the built-in list. The current value
/// is always among the options.
public struct ConfigOptions: Codable, Sendable, Equatable {
    public var key: String
    public var current: String?
    public var options: [ConfigOption]
    /// Who gave the list: `openai`, `anthropic`, `codex`, `server`, `catalog` or `builtin`.
    public var source: String
    /// The list came from `source` just now, not from a list built in.
    public var live: Bool
    /// A value not in the list may be typed in.
    public var freeForm: Bool
    /// Why the live list could not be read.
    public var error: String?

    public init(key: String, current: String?, options: [ConfigOption], source: String, live: Bool,
                freeForm: Bool, error: String?) {
        self.key = key
        self.current = current
        self.options = options
        self.source = source
        self.live = live
        self.freeForm = freeForm
        self.error = error
    }
}

/// One value a setting may take, and how a person reads it.
public struct ConfigOption: Codable, Sendable, Equatable, Hashable, Identifiable {
    public var value: String
    public var label: String
    /// A word about it: "current" for a value the live list lacks, a voice's sound.
    public var detail: String?

    public var id: String { value }

    public init(value: String, label: String, detail: String? = nil) {
        self.value = value
        self.label = label
        self.detail = detail
    }
}
