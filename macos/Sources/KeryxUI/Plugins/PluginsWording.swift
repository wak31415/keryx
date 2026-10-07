import KeryxKit
import SwiftUI

/// What an off plugin's fields hold until it is turned on: `plugins set` refuses a plugin
/// that is not installed, so its values go with `plugins install` instead.
struct PluginDraft: Equatable {
    /// `KEY=VALUE` for each field changed, as it will be written.
    var settings: [String: String] = [:]
    /// The whole `[clusters]` table, once it has been changed.
    var clusters: [String: String]?

    var isEmpty: Bool { settings.isEmpty && clusters == nil }

    /// What `plugins install` is handed.
    var values: PluginValues {
        PluginValues(settings: settings, clusters: clusters ?? [:])
    }
}

/// A value typed into a field that is not one: said under the field, never sent.
struct FieldProblem: Error, Equatable {
    var message: String
}

/// The Plugins screen's words and decisions, kept apart from the views so they can be
/// tested.
enum PluginsWording {
    // MARK: State

    /// The dot and word beside the switch: on (installed and loading), not loading
    /// (installed and refused), or off.
    static func status(_ plugin: Plugin) -> (tone: StatusDot.Tone, text: String) {
        if plugin.on { return plugin.ready ? (.ok, "On") : (.warning, "On, but not ready") }
        if plugin.installed { return (.warning, "On, but not loading") }
        return (.inactive, "Off")
    }

    /// The sentence under the status: why it would not work, or that it will.
    static func detail(_ plugin: Plugin, assistant: String?) -> String? {
        if let why = plugin.why { return why }
        if plugin.on { return "\(assistant ?? "The assistant") can use it from the next call." }
        if plugin.installed, let refused = plugin.refused { return refusal(refused) }
        return nil
    }

    /// The accounts it needs, when none of them is connected (any one will do); empty when
    /// one is, when it needs none, or before the accounts are read.
    static func missingAccounts(_ plugin: Plugin, integrations: IntegrationsReport?) -> [Integration] {
        guard let integrations, !plugin.needs.isEmpty else { return [] }
        let needed = plugin.needs.compactMap(integrations.integration)
        return needed.contains(where: \.connected) ? [] : needed
    }

    /// "Connect Gmail First"; with a choice of accounts, Integrations itself.
    static func connectFirst(_ missing: [Integration]) -> String? {
        guard let first = missing.first else { return nil }
        return missing.count == 1 ? "Connect \(Wording.titleCase(first.title)) First" : "Go to Integrations"
    }

    /// Asked before a plugin is turned off.
    static func turnOffQuestion(_ plugin: Plugin, assistant: String?) -> IntegrationQuestion {
        IntegrationQuestion(
            title: "Turn Off \(plugin.title)?",
            message: "\(assistant ?? "The assistant") stops offering it from the next call. Its settings are kept "
                + "for when you turn it on again.",
            button: "Turn Off")
    }

    /// A change that failed, as the card says it: the CLI names plugins by their tool
    /// (`send_to_slack`), the card by their title.
    static func failure(_ message: String, plugins: PluginsReport?) -> String {
        var text = message
        for plugin in plugins?.plugins ?? [] {
            text = text.replacingOccurrences(of: plugin.name, with: plugin.title)
        }
        return Wording.sentence(Wording.capitalised(text))
    }

    // MARK: Fields

    /// The fields asked first, and the rest (under More Settings).
    static func split(_ fields: [PluginField]) -> (important: [PluginField], more: [PluginField]) {
        (fields.filter(\.important), fields.filter { !$0.important })
    }

    /// A field's value as its editor holds it: a whole number without `.0`, a bool as
    /// `true`/`false`.
    static func text(_ value: JSONValue) -> String {
        if case let .bool(on) = value { return on ? "true" : "false" }
        return value.displayText
    }

    /// What `text` typed into a field of `kind` is written as; a problem for a value that
    /// cannot be one. Every number the CLI has is a `float`, whole ones included.
    static func parse(_ text: String, as kind: PluginFieldType?) -> Result<String, FieldProblem> {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.contains(where: \.isNewline) { return .failure(FieldProblem(message: "Use one line.")) }
        switch kind {
        case .int:
            guard let number = Int(trimmed), number >= 0 else {
                return .failure(FieldProblem(message: "Enter a whole number, 0 or more."))
            }
            return .success(String(number))
        case .float:
            guard let number = Double(trimmed), number.isFinite, number >= 0 else {
                return .failure(FieldProblem(message: "Enter a number, 0 or more."))
            }
            return .success(JSONValue.number(number).displayText)
        case .bool:
            return .success(["true", "yes", "on", "1"].contains(trimmed.lowercased()) ? "true" : "false")
        case .string, .choice, .map, nil:
            return .success(trimmed)
        }
    }

    /// A choice as the menu shows it: the plugins' own words for each ("openai" is
    /// "OpenAI"), any other capitalised.
    static func choiceTitle(_ choice: String) -> String {
        let known = ["auto": "Automatic", "openai": "OpenAI", "anthropic": "Anthropic"]
        return known[choice] ?? Wording.capitalised(choice)
    }

    /// A `map` field's table, by alias.
    static func table(_ value: JSONValue) -> [String: String] {
        guard case let .object(entries) = value else { return [:] }
        return entries.mapValues(\.displayText)
    }

    /// The ssh hosts a cluster can be added from: only one with a ControlMaster (Cluster
    /// stats never opens a connection of its own), and not one already in the table.
    static func clusterHosts(_ report: SshHostsReport?, taken: Set<String>) -> [SshHost] {
        (report?.hosts ?? []).filter { $0.controlMaster && !taken.contains($0.alias.lowercased()) }
    }

    /// A partition is a bare word, as the CLI wants it.
    static func isBareWord(_ text: String) -> Bool {
        !text.isEmpty && text.allSatisfy { $0.isASCII && ($0.isLetter || $0.isNumber || "_.-".contains($0)) }
    }

    // MARK: The owner's own tools

    /// The owner's own tools and refusals: those of the plugins' files are on the plugins'
    /// cards already.
    static func ownTools(_ tools: ToolsReport, plugins: PluginsReport?) -> (tools: [CustomTool], refused: [RefusedTool]) {
        let pluginFiles = Set((plugins?.plugins ?? []).map { ($0.toolFile as NSString).lastPathComponent })
        let builtIn = Set((plugins?.plugins ?? []).map(\.name))
        return (tools.tools.filter { !pluginFiles.contains($0.file) && !builtIn.contains($0.name) },
                tools.refused.filter { !pluginFiles.contains($0.file) })
    }

    /// A tool's description is written for the model ("Say that it is opening…"): its first
    /// sentence is what it does.
    static func firstSentence(_ text: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let end = trimmed.range(of: #"[.!?](\s|$)"#, options: .regularExpression) else { return trimmed }
        return String(trimmed[..<end.upperBound]).trimmingCharacters(in: .whitespaces)
    }

    /// Why the next call refuses a file, as the loader said it, less any command it quotes
    /// (the remedy is a button here, never a command).
    static func refusal(_ reason: String) -> String {
        let cause = reason.replacingOccurrences(of: #"\s*:?\s*`[^`]*`.*$"#, with: "", options: .regularExpression)
        return Wording.sentence(Wording.capitalised(cause.isEmpty ? reason : cause))
    }

    // MARK: Looks

    static func symbol(_ name: String) -> String {
        switch name {
        case "send_to_slack": "paperplane.fill"
        case "check_email": "envelope.open.fill"
        case "check_billing": "dollarsign"
        case "cluster_stats": "server.rack"
        default: "puzzlepiece.extension.fill"
        }
    }

    static func tint(_ name: String) -> Color {
        switch name {
        case "send_to_slack": .purple
        case "check_email": .red
        case "check_billing": .green
        case "cluster_stats": .indigo
        default: .gray
        }
    }
}
