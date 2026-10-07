import KeryxKit
import SwiftUI

/// The main window: the sidebar of sections, and the section chosen. While Keryx cannot be
/// found, or has files to move first, that takes the detail's place whatever is selected.
public struct RootView: View {
    @Environment(AppModel.self) private var model

    public init() {}

    public var body: some View {
        @Bindable var model = model
        NavigationSplitView {
            List(SidebarItem.allCases, selection: $model.selection) { item in
                Label(item.title, systemImage: item.systemImage)
                    .tag(item)
            }
            .navigationSplitViewColumnWidth(min: 180, ideal: 200, max: 260)
        } detail: {
            detail
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    Task { await model.refresh(.all) }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .help("Refresh")
                .keyboardShortcut("r", modifiers: .command)
            }
        }
        .navigationTitle(onboarding ? "Keryx" : model.selection.title)
        .navigationSubtitle(onboarding ? "" : subtitle)
        .frame(minWidth: 860, minHeight: 560)
    }

    @ViewBuilder private var detail: some View {
        switch model.menuState {
        case .notFound:
            LocateView()
        case .needsMigration:
            MigrationView()
        default:
            switch model.selection {
            case .overview: OverviewView()
            case .sessions: SessionsView()
            case .models: ModelsView()
            case .configuration: ConfigurationView()
            }
        }
    }

    /// Keryx is not found, or must move its files first: one screen stands in for every
    /// section.
    private var onboarding: Bool {
        model.menuState == .notFound || model.menuState == .needsMigration
    }

    /// "Lyra · running", once the status has named the assistant.
    private var subtitle: String {
        let parts = [Wording.assistant(model.status.value), Wording.shortStatus(model.menuState)]
        return parts.compactMap { $0 }.joined(separator: " · ")
    }
}
