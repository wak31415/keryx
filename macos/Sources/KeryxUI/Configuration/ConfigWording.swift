import Foundation
import KeryxKit

/// How the Configuration screen names, orders, filters and edits `keryx config list`'s rows.
enum ConfigWording {
    /// One `Section` of the screen: a group's rows, in the order `config list` gave them.
    struct Group: Identifiable {
        let group: String
        let rows: [ConfigRow]

        var id: String { group }
        /// The CLI's title for the group, from its first row, else this build's own.
        var title: String { rows.first?.displayGroupTitle ?? ConfigWording.title(ofGroup: group) }
    }

    /// A group's title when the CLI does not name it (an older CLI).
    static func title(ofGroup group: String) -> String {
        ConfigRow.title(ofGroup: group)
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

    /// The rows whose title, key or description contains `query`, ignoring case; every row
    /// for a blank query.
    static func filter(_ rows: [ConfigRow], by query: String) -> [ConfigRow] {
        let query = query.trimmingCharacters(in: .whitespaces)
        guard !query.isEmpty else { return rows }
        return rows.filter {
            $0.displayTitle.localizedCaseInsensitiveContains(query)
                || $0.key.localizedCaseInsensitiveContains(query)
                || $0.description.localizedCaseInsensitiveContains(query)
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

    /// Why a row is read-only, naming the setting and the variable to unset.
    static func environmentHelp(_ row: ConfigRow) -> String {
        "\(row.displayTitle) is set in the environment as \(row.key), which wins over the store"
    }

    /// The CLI's sentence for a refused write, with the setting it opens on ("VAD_SILENCE_MS
    /// Input should be…") named by its title instead: "Silence before a turn ends (ms): input
    /// should be…". Any other sentence is shown as the CLI wrote it.
    static func errorText(_ message: String, for row: ConfigRow) -> String {
        errorText(message, key: row.key, title: row.displayTitle)
    }

    static func errorText(_ message: String, key: String, title: String) -> String {
        guard title != key, message.hasPrefix(key + " ") else { return message }
        let rest = message.dropFirst(key.count + 1)
        return "\(title): " + rest.prefix(1).lowercased() + rest.dropFirst()
    }

    /// The phone PIN: listed by `config list`, but not a setting. It lives in
    /// `KERYX_HOME/pin`, is written once, and `config set KERYX_PIN` is always refused; the
    /// one keyboard path that may replace it is `keryx setup` in Terminal. The app has no PIN
    /// UI, so this row shows only whether one is set.
    static func isPIN(_ row: ConfigRow) -> Bool {
        row.key == "KERYX_PIN"
    }

    /// Whether the row offers any write at all — a control, Set…/Change…/Remove, or Reset
    /// to Default. Not the environment's rows, and never the PIN.
    static func offersWrites(_ row: ConfigRow) -> Bool {
        !readOnly(row) && !isPIN(row)
    }

    static let pinNote = "Change it with Keryx Setup in Terminal"

    /// Whether a field's text should be sent: it differs from what is stored, no write is
    /// under way, and it is not the text the CLI last refused (unchanged since).
    static func shouldCommit(_ text: String, stored: String, writing: Bool, refused: String?) -> Bool {
        !writing && text != stored && text != refused
    }

    /// Unsetting a secret throws away something Keryx cannot show again, so it is asked
    /// about first; any other setting goes back to its default in one click.
    static func resetNeedsConfirmation(_ row: ConfigRow) -> Bool {
        row.secret
    }

    /// "Remove OpenAI API key?"
    static func removalTitle(_ row: ConfigRow) -> String {
        "Remove \(row.displayTitle)?"
    }

    static let secretRemovalMessage =
        "Keryx can't show a secret again, so to use it later you'll have to paste it in again."

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
