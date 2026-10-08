import KeryxKit
import SwiftUI

/// The advanced settings a screen owns (`config list`'s `screen`), under a collapsed
/// Advanced disclosure at the foot of that screen: Voice's `VOICE_SERVER_ARGS`, Local Models'
/// ports. They are on no Configuration tab, so this is their one editor. Nothing is drawn
/// before the settings are read, or for a screen that owns no advanced setting.
struct ScreenAdvancedSection: View {
    let screen: SidebarItem

    @Environment(AppModel.self) private var model
    @State private var open: Bool

    /// `open`: whether the disclosure starts open (the catalog's).
    init(screen: SidebarItem, open: Bool = false) {
        self.screen = screen
        _open = State(initialValue: open)
    }

    var body: some View {
        let all = model.config.value ?? []
        let rows = Self.rows(all, on: screen)
        if !rows.isEmpty {
            let titles = Wording.titles(all)
            Section {
                DisclosureGroup(isExpanded: $open) {
                    ForEach(Array(rows.enumerated()), id: \.element.id) { index, row in
                        if index > 0 { Divider() }
                        ConfigRowView(row: row, titles: titles)
                            .padding(.vertical, 4)
                    }
                } label: {
                    LabeledContent("Advanced") {
                        Text(rows.count == 1 ? "1 setting" : "\(rows.count) settings")
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    /// The advanced rows `screen` owns, in `config list`'s order.
    static func rows(_ rows: [ConfigRow], on screen: SidebarItem) -> [ConfigRow] {
        rows.filter { $0.screen == screen.rawValue && $0.advanced }
    }
}
