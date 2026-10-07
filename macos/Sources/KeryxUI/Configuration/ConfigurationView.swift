import KeryxKit
import SwiftUI

/// Every setting `keryx config list` shows, by group, each with the control its type wants.
/// The groups an owner tunes most start open and the rest closed; a search opens every
/// group it matches in. Changes go through the CLI (`AppModel.set`, `setSecret`, `unset`);
/// once one lands, a banner at the top offers the restart that loads it.
public struct ConfigurationView: View {
    @Environment(AppModel.self) private var model
    @State private var search: String
    /// The groups open now.
    @State private var open: Set<String>
    /// Errors to show under rows when the screen first appears (the catalog's write error).
    private let initialErrors: [String: KeryxError]

    public init() {
        self.init(search: "")
    }

    package init(search: String, errors: [String: KeryxError] = [:], open: Set<String> = ConfigWording.openGroups) {
        _search = State(initialValue: search)
        _open = State(initialValue: open)
        initialErrors = errors
    }

    public var body: some View {
        StateView(model.config, section: "Configuration", retry: { Task { await model.refresh(.config) } }) { rows in
            let groups = ConfigWording.groups(ConfigWording.filter(rows, by: search))
            let titles = Wording.titles(rows)
            if groups.isEmpty, !rows.isEmpty {
                ContentUnavailableView.search(text: search)
            } else {
                Form {
                    if model.configNeedsRestart {
                        RestartBanner()
                    }
                    ForEach(groups) { group in
                        Section(group.title, isExpanded: expanded(group.group)) {
                            ForEach(group.rows) { row in
                                ConfigRowView(row: row, titles: titles, error: initialErrors[row.key])
                            }
                        }
                    }
                }
                .formStyle(.grouped)
            }
        }
        .searchable(text: $search, prompt: "Search Settings")
        .task { await model.refresh(.config) }
    }

    private var searching: Bool {
        !search.trimmingCharacters(in: .whitespaces).isEmpty
    }

    /// Whether a group is open: always while a search is shown, so every match is seen.
    private func expanded(_ group: String) -> Binding<Bool> {
        Binding {
            searching || open.contains(group)
        } set: { isOpen in
            if isOpen { open.insert(group) } else { open.remove(group) }
        }
    }
}
