import Foundation
import KeryxKit

/// How the Configuration screen names, orders, filters and edits `keryx config list`'s rows.
enum ConfigWording {
    /// One `Section` of the screen: a group's rows, in the order `config list` gave them.
    struct Group: Identifiable {
        let group: String
        let rows: [ConfigRow]

        var id: String { group }
        var title: String { ConfigWording.title(ofGroup: group) }
    }

    /// The section title for a settings group; one this build does not know is shown as the
    /// CLI names it, capitalised.
    static func title(ofGroup group: String) -> String {
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
        default: Wording.capitalised(group)
        }
    }

    /// The rows by group, each group where `config list` first names it.
    static func groups(_ rows: [ConfigRow]) -> [Group] {
        var order: [String] = []
        var byGroup: [String: [ConfigRow]] = [:]
        for row in rows {
            if byGroup[row.group] == nil { order.append(row.group) }
            byGroup[row.group, default: []].append(row)
        }
        return order.map { Group(group: $0, rows: byGroup[$0] ?? []) }
    }

    /// The rows whose key or description contains `query`, ignoring case; every row for a
    /// blank query.
    static func filter(_ rows: [ConfigRow], by query: String) -> [ConfigRow] {
        let query = query.trimmingCharacters(in: .whitespaces)
        guard !query.isEmpty else { return rows }
        return rows.filter {
            $0.key.localizedCaseInsensitiveContains(query) || $0.description.localizedCaseInsensitiveContains(query)
        }
    }

    /// Where a value comes from, when it is not the default: "from config.toml".
    static func source(_ source: String) -> String? {
        switch source {
        case "default": nil
        case "environment": "from the environment"
        case "command line": "from the command line"
        default: "from \(source)"
        }
    }

    /// A row the app cannot change: the environment wins over anything the store holds.
    /// `protected` binds only the running service, never the owner.
    static func readOnly(_ row: ConfigRow) -> Bool {
        row.source == "environment"
    }

    static let environmentHelp = "Set in the environment, which wins over the store"

    /// A value as the owner types it back: a list comma-separated (the CLI splits on commas
    /// and trims), a whole number without `.0`, nothing for no value.
    static func editText(_ value: JSONValue?) -> String {
        switch value {
        case nil, .null?: ""
        case let .array(items)?: items.map(\.displayText).joined(separator: ", ")
        case let value?: value.displayText
        }
    }

    /// A description with its `code` spans set in the system's code style.
    static func description(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }

    /// A description as plain text, for a tooltip.
    static func plain(_ text: String) -> String {
        text.replacingOccurrences(of: "`", with: "")
    }
}
