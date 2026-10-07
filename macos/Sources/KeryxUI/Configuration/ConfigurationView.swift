import KeryxKit
import SwiftUI

/// Every setting `keryx config list` shows, by group, each with the control its type wants.
/// Changes go through the CLI (`AppModel.set`, `setSecret`, `unset`); once one lands, a
/// banner at the top offers the restart that loads it.
public struct ConfigurationView: View {
    @Environment(AppModel.self) private var model
    @State private var search: String
    /// Errors to show under rows when the screen first appears (the catalog's write error).
    private let initialErrors: [String: KeryxError]

    public init() {
        self.init(search: "")
    }

    init(search: String, errors: [String: KeryxError] = [:]) {
        _search = State(initialValue: search)
        initialErrors = errors
    }

    public var body: some View {
        StateView(model.config, retry: { Task { await model.refresh(.config) } }) { rows in
            let groups = ConfigWording.groups(ConfigWording.filter(rows, by: search))
            if groups.isEmpty, !rows.isEmpty {
                ContentUnavailableView.search(text: search)
            } else {
                Form {
                    if model.configNeedsRestart {
                        RestartBanner()
                    }
                    ForEach(groups) { group in
                        Section(group.title) {
                            ForEach(group.rows) { row in
                                ConfigRowView(row: row, error: initialErrors[row.key])
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
}
