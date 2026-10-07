import Foundation
import KeryxKit

/// How Configuration lays `keryx config list`'s rows out on tabs: each row's `tab`, the
/// tabs in `tab_order`, and each tab's key settings apart from its advanced ones. Nothing
/// here keeps a list of which setting is on which tab — the CLI says.
extension ConfigWording {
    /// One tab: its key settings by group, and the rest by group under its Advanced
    /// disclosure.
    struct Tab: Identifiable {
        let id: String
        let title: String
        let order: Int
        /// The key settings (`advanced == false`), by group in `config list`'s order.
        let groups: [Group]
        /// The advanced settings, by group in `config list`'s order.
        let advanced: [Group]

        /// The SF Symbol in the tab bar.
        var symbol: String { ConfigWording.symbol(ofTab: id) }
        /// Every row on the tab, key settings first.
        var rows: [ConfigRow] { (groups + advanced).flatMap(\.rows) }
        var keyRows: [ConfigRow] { groups.flatMap(\.rows) }
        var advancedRows: [ConfigRow] { advanced.flatMap(\.rows) }
    }

    /// The rows by tab, in `tab_order` (ties in the order `config list` first names them).
    static func tabs(_ rows: [ConfigRow]) -> [Tab] {
        var order: [String] = []
        var byTab: [String: [ConfigRow]] = [:]
        for row in rows {
            if byTab[row.tab] == nil { order.append(row.tab) }
            byTab[row.tab, default: []].append(row)
        }
        let tabs = order.map { id -> Tab in
            let rows = byTab[id] ?? []
            let first = rows[0]
            return Tab(id: id, title: Wording.titleCase(first.tabTitle), order: first.tabOrder,
                       groups: groups(rows.filter { !$0.advanced }),
                       advanced: groups(rows.filter(\.advanced)))
        }
        return tabs.enumerated()
            .sorted { ($0.element.order, $0.offset) < ($1.element.order, $1.offset) }
            .map(\.element)
    }

    /// The tab to show: the one chosen, while it is still there, else the first.
    static func selected(_ id: String?, in tabs: [Tab]) -> Tab? {
        tabs.first { $0.id == id } ?? tabs.first
    }

    /// A search's matches across every tab, advanced settings included, by tab.
    static func results(_ rows: [ConfigRow], for query: String) -> [Tab] {
        tabs(filter(rows, by: query))
    }

    /// A group's section title on its tab, or none where it would only repeat the tab's
    /// name (the Voice group on the Voice tab).
    static func sectionTitle(_ group: Group, on tab: Tab) -> String? {
        group.title.caseInsensitiveCompare(tab.title) == .orderedSame ? nil : group.title
    }

    /// An advanced group's section title, under the Advanced disclosure: its group's name
    /// where the tab has settings from more than one group, else none.
    static func advancedSectionTitle(_ group: Group, on tab: Tab) -> String? {
        Set(tab.rows.map(\.group)).count > 1 ? group.title : nil
    }

    /// The Advanced disclosure's label: "Advanced", and how many settings it holds.
    static func advancedCount(_ tab: Tab) -> String {
        let count = tab.advancedRows.count
        return count == 1 ? "1 setting" : "\(count) settings"
    }

    /// A search result's section title: the tab, and "Advanced" after it for the tab's
    /// advanced settings ("Voice › Advanced").
    static func resultTitle(_ tab: Tab, advanced: Bool) -> String {
        advanced ? "\(tab.title) › Advanced" : tab.title
    }

    /// The tab bar's symbol for each tab the CLI names; a tab this build does not know gets
    /// a plain one.
    static func symbol(ofTab tab: String) -> String {
        switch tab {
        case "general": "gearshape"
        case "voice": "waveform"
        case "agents": "cpu"
        case "local": "desktopcomputer"
        case "phone": "phone"
        case "approvals": "checkmark.shield"
        case "integrations": "puzzlepiece.extension"
        case "system": "wrench.and.screwdriver"
        default: "ellipsis.circle"
        }
    }
}
