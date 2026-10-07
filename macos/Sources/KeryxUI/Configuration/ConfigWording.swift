import Foundation
import KeryxKit

/// How the Configuration screen names, orders, filters and edits `keryx config list`'s rows.
enum ConfigWording {
    /// One `Section` of the screen: a group's rows, in the order `config list` gave them.
    struct Group: Identifiable {
        let group: String
        let rows: [ConfigRow]

        var id: String { group }
        /// The CLI's title for the group, from its first row, else this build's own — in
        /// title case, as a section's title is.
        var title: String { Wording.titleCase(rows.first?.displayGroupTitle ?? ConfigWording.title(ofGroup: group)) }
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

    /// What to show for a refused write: Keryx's own sentence, with the two validation
    /// failures it words for a programmer ("Input should be a valid integer…") said as what
    /// to do, and the setting it opens on ("VAD_SILENCE_MS Input should be…") named by its
    /// title instead. Any other sentence is shown as the CLI wrote it, as a sentence.
    static func errorText(_ error: KeryxError, for row: ConfigRow) -> String {
        guard let reported = error.reported else { return error.message }
        return errorText(reported, key: row.key, title: row.displayTitle)
    }

    static func errorText(_ message: String, for row: ConfigRow) -> String {
        errorText(message, key: row.key, title: row.displayTitle)
    }

    static func errorText(_ message: String, key: String, title: String) -> String {
        if message.localizedCaseInsensitiveContains("valid integer") { return wholeNumber }
        if message.localizedCaseInsensitiveContains("valid number") { return number }
        guard title != key, message.hasPrefix(key + " ") else { return Wording.sentence(message) }
        let rest = message.dropFirst(key.count + 1)
        return Wording.sentence("\(title): " + rest.prefix(1).lowercased() + rest.dropFirst())
    }

    static let wholeNumber = "Enter a whole number."
    static let number = "Enter a number."

    /// Why `text` cannot be a value of a number setting, checked before it is sent; `nil`
    /// when it can (or the setting is not a number).
    static func invalid(_ text: String, type: String) -> String? {
        let text = text.trimmingCharacters(in: .whitespaces)
        switch type {
        case "int": return Int(text) == nil ? wholeNumber : nil
        case "float": return Double(text) == nil ? number : nil
        default: return nil
        }
    }

    // MARK: - Sections

    /// The groups open when the screen first appears: what an owner tunes most. The rest
    /// start closed, and a search opens every group it matches in.
    static let openGroups: Set<String> = ["voice", "agents", "local", "owner"]

    // MARK: - Choices

    /// The settings whose choices are the coding agents, shown by their names.
    static let agentKeys: Set<String> = ["AGENT_BACKEND", "AGENTS_ENABLED"]

    /// A choice as a person reads it: an agent by its name ("Claude", "Local model"), any
    /// other with a capital first letter and its underscores and hyphens as spaces
    /// ("near_field" → "Near field").
    static func choiceTitle(_ choice: String, for row: ConfigRow) -> String {
        if agentKeys.contains(row.key) { return Wording.agent(choice) }
        let spaced = choice.replacingOccurrences(of: "_", with: " ").replacingOccurrences(of: "-", with: " ")
        return Wording.capitalised(spaced)
    }

    /// A list setting with a fixed set of choices (`AGENTS_ENABLED`): a checkbox per choice.
    static func isChecklist(_ row: ConfigRow) -> Bool {
        row.type == "list" && !(row.choices ?? []).isEmpty
    }

    /// The choices a checklist row has on: its list, else — blank — `fallback` (the default
    /// agent, for `AGENTS_ENABLED`, which blank means alone).
    static func checked(_ value: JSONValue?, fallback: String?) -> [String] {
        let listed: [String] = if case let .array(items)? = value { items.map(\.displayText) } else { [] }
        if listed.isEmpty, let fallback, !fallback.isEmpty { return [fallback] }
        return listed
    }

    /// The comma-separated value with `choice` turned on or off, in the choices' order. A
    /// choice already listed that is not among them stays, after them.
    static func toggling(_ choice: String, on: Bool, in checked: [String], choices: [String]) -> String {
        var set = Set(checked)
        if on { set.insert(choice) } else { set.remove(choice) }
        let ordered = choices.filter(set.contains) + checked.filter { !choices.contains($0) && set.contains($0) }
        return ordered.joined(separator: ",")
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

    /// Under the PIN's button: whether one is set, and that Keryx Setup is where it changes.
    static func pinCaption(_ row: ConfigRow) -> String {
        row.set ? "Set · change it in Keryx Setup" : "Not set · set it in Keryx Setup"
    }

    /// Under a secret's buttons: whether it is set, and where it is kept. "Set · from
    /// secrets.toml".
    static func secretCaption(_ row: ConfigRow) -> String {
        guard row.set else { return "Not set" }
        return ["Set", source(row.source)].compactMap { $0 }.joined(separator: " · ")
    }

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

    /// A row's tooltip: its whole description, then the key `keryx config set` takes.
    static func tooltip(_ row: ConfigRow) -> String {
        let description = plain(row.description)
        return description.isEmpty ? row.key : description + "\n\n" + row.key
    }

    /// A description as plain text: on one line under the title, and whole in its tooltip.
    static func plain(_ text: String) -> String {
        text.replacingOccurrences(of: "`", with: "")
    }
}
