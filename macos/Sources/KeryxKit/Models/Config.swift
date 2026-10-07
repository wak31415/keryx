import Foundation

/// One row of `keryx config list --json`. `value` is `nil` for a secret, whether set or not
/// — `config list` never prints one — and for anything unset with no default.
///
/// `title` and `groupTitle` are the names a person reads ("OpenAI API key", "Local and
/// self-hosted models"); a CLI older than them omits both, so they are optional and the
/// `display…` properties fall back to the key and to this build's own group titles.
///
/// `tab`, `tabTitle`, `tabOrder` and `advanced` place the row on Configuration's tabs: which
/// tab, its name and position, and whether it waits under the tab's Advanced disclosure. A
/// CLI older than them omits all four; the row is then placed by its group
/// (`tab(ofGroup:)`) and counts as a key setting.
public struct ConfigRow: Codable, Sendable, Equatable, Identifiable {
    public var key: String
    public var group: String
    public var title: String?
    public var groupTitle: String?
    /// The tab the row is on (`general`, `voice`, … `system`).
    public var tab: String
    public var tabTitle: String
    /// Where the tab sits in the bar, first at 0.
    public var tabOrder: Int
    /// Shown under the tab's Advanced disclosure rather than among its key settings.
    public var advanced: Bool
    public var description: String
    public var source: String
    public var type: String
    public var required: Bool
    public var set: Bool
    public var secret: Bool
    public var serviceWritable: Bool
    public var protected: Bool
    public var value: JSONValue?
    public var choices: [String]?
    /// `keryx config options KEY` can list what it may be set to (a model, a voice). A CLI
    /// older than that command omits it, which reads as false.
    public var hasOptions: Bool = false

    public var id: String { key }

    /// Every field, with the four tab placements optional: left out, they follow `group` as
    /// an older CLI's row does.
    public init(key: String, group: String, title: String? = nil, groupTitle: String? = nil,
                tab: String? = nil, tabTitle: String? = nil, tabOrder: Int? = nil, advanced: Bool = false,
                description: String, source: String, type: String, required: Bool, set: Bool,
                secret: Bool, serviceWritable: Bool, protected: Bool, value: JSONValue?,
                choices: [String]?, hasOptions: Bool = false) {
        self.key = key
        self.group = group
        self.title = title
        self.groupTitle = groupTitle
        let tab = tab ?? Self.tab(ofGroup: group)
        self.tab = tab
        self.tabTitle = tabTitle ?? Self.title(ofTab: tab, group: group)
        self.tabOrder = tabOrder ?? Self.order(ofTab: tab)
        self.advanced = advanced
        self.description = description
        self.source = source
        self.type = type
        self.required = required
        self.set = set
        self.secret = secret
        self.serviceWritable = serviceWritable
        self.protected = protected
        self.value = value
        self.choices = choices
        self.hasOptions = hasOptions
    }

    /// What the setting is called on screen: the CLI's title, else the key itself.
    public var displayTitle: String {
        guard let title, !title.isEmpty else { return key }
        return title
    }

    /// The section the setting is shown in: the CLI's group title, else `title(ofGroup:)`.
    public var displayGroupTitle: String {
        guard let groupTitle, !groupTitle.isEmpty else { return Self.title(ofGroup: group) }
        return groupTitle
    }

    /// A group's title for a CLI that does not name it; a group this build does not know is
    /// shown as the CLI names it, capitalised.
    public static func title(ofGroup group: String) -> String {
        switch group {
        case "voice": "Voice"
        case "agents": "Coding Agents"
        case "local": "Local Models"
        case "phone": "Phone"
        case "owner": "You"
        case "projects": "Projects"
        case "limits": "Limits"
        case "approvals": "Approvals"
        case "plugins": "Plugins"
        case "google": "Google"
        case "service": "Service"
        case "debug": "Debugging"
        default: group.prefix(1).uppercased() + group.dropFirst()
        }
    }

    /// The tabs a CLI that names them uses, in their order.
    public static let knownTabs = ["general", "voice", "agents", "local", "phone", "approvals", "integrations", "system"]

    /// The tab a group's rows go on when the CLI does not say (an older CLI): the tab the
    /// CLI puts that group on now, else a tab of the group's own.
    public static func tab(ofGroup group: String) -> String {
        switch group {
        case "owner": "general"
        case "voice", "agents", "local", "phone", "approvals": group
        case "google", "plugins", "projects": "integrations"
        case "limits", "service", "debug": "system"
        default: group
        }
    }

    /// A tab's name when the CLI does not give one; a tab of a group this build does not
    /// know is named as the group is.
    public static func title(ofTab tab: String, group: String? = nil) -> String {
        switch tab {
        case "general": "General"
        case "voice": "Voice"
        case "agents": "Agents"
        case "local": "Local Models"
        case "phone": "Phone"
        case "approvals": "Approvals"
        case "integrations": "Integrations"
        case "system": "System"
        default: title(ofGroup: group ?? tab)
        }
    }

    /// A tab's position when the CLI does not give one: the known tabs in their order, and
    /// any other after them.
    public static func order(ofTab tab: String) -> Int {
        knownTabs.firstIndex(of: tab) ?? knownTabs.count
    }
}

extension ConfigRow {
    /// As synthesised, but with `has_options` and `advanced` read as false when they are
    /// missing, and the tab placed by the group when the CLI does not place it.
    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        key = try container.decode(String.self, forKey: .key)
        group = try container.decode(String.self, forKey: .group)
        title = try container.decodeIfPresent(String.self, forKey: .title)
        groupTitle = try container.decodeIfPresent(String.self, forKey: .groupTitle)
        tab = try container.decodeIfPresent(String.self, forKey: .tab) ?? Self.tab(ofGroup: group)
        tabTitle = try container.decodeIfPresent(String.self, forKey: .tabTitle) ?? Self.title(ofTab: tab, group: group)
        tabOrder = try container.decodeIfPresent(Int.self, forKey: .tabOrder) ?? Self.order(ofTab: tab)
        advanced = try container.decodeIfPresent(Bool.self, forKey: .advanced) ?? false
        description = try container.decode(String.self, forKey: .description)
        source = try container.decode(String.self, forKey: .source)
        type = try container.decode(String.self, forKey: .type)
        required = try container.decode(Bool.self, forKey: .required)
        set = try container.decode(Bool.self, forKey: .set)
        secret = try container.decode(Bool.self, forKey: .secret)
        serviceWritable = try container.decode(Bool.self, forKey: .serviceWritable)
        protected = try container.decode(Bool.self, forKey: .protected)
        value = try container.decodeIfPresent(JSONValue.self, forKey: .value)
        choices = try container.decodeIfPresent([String].self, forKey: .choices)
        hasOptions = try container.decodeIfPresent(Bool.self, forKey: .hasOptions) ?? false
    }
}

/// A setting's value, or any other loosely-typed JSON the CLI hands back (`UseResult.saved`,
/// an approval's `requestId`), decoded and re-encoded without losing its shape.
public enum JSONValue: Codable, Equatable, Sendable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case array([JSONValue])
    case object([String: JSONValue])
    case null

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "Unsupported JSON value")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case let .string(value): try container.encode(value)
        case let .number(value): try container.encode(value)
        case let .bool(value): try container.encode(value)
        case let .array(value): try container.encode(value)
        case let .object(value): try container.encode(value)
        case .null: try container.encodeNil()
        }
    }

    /// How a value reads in a plain line of text: a string as-is, a whole number without a
    /// trailing `.0`, a bool as `On`/`Off`, an array comma-joined, null as nothing at all.
    public var displayText: String {
        switch self {
        case let .string(value):
            return value
        case let .number(value):
            if value.truncatingRemainder(dividingBy: 1) == 0, abs(value) < 1e15 {
                return String(Int64(value))
            }
            return String(value)
        case let .bool(value):
            return value ? "On" : "Off"
        case let .array(value):
            return value.map(\.displayText).joined(separator: ", ")
        case .object:
            return ""
        case .null:
            return ""
        }
    }
}
