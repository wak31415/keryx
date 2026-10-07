import Foundation

/// One row of `keryx config list --json`. `value` is `nil` for a secret, whether set or not
/// — `config list` never prints one — and for anything unset with no default.
///
/// `title` and `groupTitle` are the names a person reads ("OpenAI API key", "Local and
/// self-hosted models"); a CLI older than them omits both, so they are optional and the
/// `display…` properties fall back to the key and to this build's own group titles.
public struct ConfigRow: Codable, Sendable, Equatable, Identifiable {
    public var key: String
    public var group: String
    public var title: String?
    public var groupTitle: String?
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

    public var id: String { key }

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
