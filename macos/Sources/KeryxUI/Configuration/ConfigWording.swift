import Foundation
import KeryxKit

/// How the Configuration screen names, orders, filters and edits `keryx config list`'s rows.
enum ConfigWording {
    /// One `Section` of a tab: a group's rows, in the order `config list` gave them.
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
                || ConfigDescriptions.text($0).localizedCaseInsensitiveContains(query)
        }
    }

    /// Where a value comes from, said only when it matters: the environment, which wins
    /// over anything set here. A value from the store is simply the value.
    static func source(_ source: String) -> String? {
        switch source {
        case "environment": "Overridden by the environment"
        case "command line": "Set on the command line"
        default: nil
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

    /// What to show for a refused write: the two validation failures Keryx words for a
    /// programmer ("Input should be a valid integer…") said as what to do; anything else the
    /// app's own sentence ("Silence before a turn ends wasn't saved."), with what Keryx said
    /// under Show Details (`KeryxError.said`).
    static func errorText(_ error: KeryxError, for row: ConfigRow) -> String {
        guard let reported = error.reported else { return error.message }
        if error.isOlderKeryx { return KeryxError.olderKeryx }
        return errorText(reported, key: row.key, title: row.displayTitle)
    }

    static func errorText(_ message: String, for row: ConfigRow) -> String {
        errorText(message, key: row.key, title: row.displayTitle)
    }

    static func errorText(_ message: String, key: String, title: String) -> String {
        if message.localizedCaseInsensitiveContains("valid integer") { return wholeNumber }
        if message.localizedCaseInsensitiveContains("valid number") { return number }
        return notSaved(title)
    }

    /// "Silence before a turn ends (ms) wasn't saved."
    static func notSaved(_ title: String) -> String {
        "\(title) wasn't saved."
    }

    /// What Keryx said of a refused write, for Show Details: not for a validation failure,
    /// which `errorText` already says as what to do.
    static func errorDetails(_ error: KeryxError) -> String? {
        guard let reported = error.reported else { return error.details }
        let validation = reported.localizedCaseInsensitiveContains("valid integer")
            || reported.localizedCaseInsensitiveContains("valid number")
        return validation ? nil : error.details
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

    /// A setting `keryx config options` can list the values of (a model, a voice): shown as
    /// a menu of them rather than a text field.
    static func hasMenu(_ row: ConfigRow) -> Bool {
        row.hasOptions && !row.secret && row.type == "string"
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
    /// `KERYX_HOME/pin`, and `config set KERYX_PIN` is always refused; the row's button opens
    /// the PIN sheet (`keryx pin set --stdin`), the keyboard path the app is.
    static func isPIN(_ row: ConfigRow) -> Bool {
        row.key == "KERYX_PIN"
    }

    /// What Configuration does for a request from another window (`RequestAnswer`): the tab
    /// to show, and the sheet to open there — the PIN's, or a coding agent's sign-in on the
    /// Agents tab; `nil` for one it does not answer. `pinTab`: the PIN row's tab, once the
    /// settings are read.
    /// For a setting asked for by its key (`.setting`), the tab it is on and the key to
    /// show there (`focus`) — `nil` until the settings are read (`config`), and the first tab
    /// for a key they do not list or put on no tab (one a screen owns, which
    /// `ConfigurationView` sends to that screen before asking here).
    static func answer(_ request: AppRequest, pinTab: String?,
                       config: [ConfigRow]? = nil) -> (tab: String, sheet: ConfigurationSheet?, focus: String?)? {
        switch RequestAnswer.of(request) {
        case let .configuration(tab, sheet):
            return (tab ?? pinTab ?? "general", sheet, nil)
        case let .setting(key):
            guard let config else { return nil }
            guard let row = config.first(where: { $0.key == key }), let tab = row.tab else {
                return (config.lazy.compactMap(\.tab).first ?? "general", nil, nil)
            }
            return (tab, nil, key)
        default:
            return nil
        }
    }

    /// A secret on the Integrations tab (an admin key, Slack's token, Google's client
    /// secret): set, changed and removed on its account's card in Integrations, not here.
    static func managedInIntegrations(_ row: ConfigRow) -> Bool {
        row.secret && row.tab == "integrations"
    }

    /// Twilio's auth token: changed in the account sheet, which checks it with its SID.
    static func isTwilioToken(_ row: ConfigRow) -> Bool {
        row.key == "TWILIO_AUTH_TOKEN"
    }

    /// Whether the row offers any write at all — a control, Add…/Change…/Remove, or Reset
    /// to Default. Not the environment's rows, never the PIN, and not a secret managed in
    /// Integrations, whose card is its one door.
    static func offersWrites(_ row: ConfigRow) -> Bool {
        !readOnly(row) && !isPIN(row) && !managedInIntegrations(row)
    }

    /// Under the PIN's button: where the PIN is (`keryx pin`), else the row's own word.
    static func pinCaption(_ row: ConfigRow, status: PinStatus?) -> String {
        PinWording.caption(status, fallbackSet: row.set)
    }

    /// Under a secret's buttons: whether it is set — and, only when it is, that the
    /// environment overrides it. Never where it is kept.
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

    /// A description as it reads under its title: plain, with the settings it names said by
    /// their titles (“Voice server URL”). The tooltip keeps the description as written.
    static func described(_ text: String, titles: [String: String]) -> String {
        Wording.named(plain(text), titles: titles)
    }

    /// A description as plain text, under the title and whole in its tooltip: no code
    /// quoting, a value's underscores as spaces (`near_field` → "near field") — a setting's
    /// key stays, for `Wording.named` to say by its title — and no emphasis (`*admin*`).
    static func plain(_ text: String) -> String {
        var result = ""
        var rest = Substring(text)
        while let open = rest.firstIndex(of: "`"),
              let close = rest[rest.index(after: open)...].firstIndex(of: "`") {
            result += rest[..<open]
            // Underscores between lower-case words only: a key in capitals stays whole.
            let code = String(rest[rest.index(after: open)..<close])
            result += code.replacingOccurrences(of: #"(?<=[a-z0-9])_(?=[a-z0-9])"#, with: " ",
                                                options: .regularExpression)
            rest = rest[rest.index(after: close)...]
        }
        result += rest.replacingOccurrences(of: "`", with: "")
        return result.replacingOccurrences(of: #"(?<![\w*])\*([^*\s][^*]*?)\*(?![\w*])"#, with: "$1",
                                           options: .regularExpression)
    }
}
