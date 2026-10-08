import KeryxKit
import SwiftUI

/// The main window: the sidebar of sections, and the section chosen. While Keryx cannot be
/// found, or has files to move first, that takes the detail's place whatever is selected.
public struct RootView: View {
    @Environment(AppModel.self) private var model

    public init() {}

    /// The section `request` only shows, which the main window answers by showing it.
    static func answers(_ request: AppRequest) -> SidebarItem? {
        if case let .shown(item) = RequestAnswer.of(request) { item } else { nil }
    }

    public var body: some View {
        @Bindable var model = model
        VStack(spacing: 0) {
            // A setting saved as its own row went (a tab, a sidebar section, or a
            // programmatic selection change) has no row left to show a refusal on; this
            // shows it over whatever section is up instead, until it is dismissed.
            if let failure = model.settingSaveFailure {
                SettingSaveFailureBanner(failure: failure)
                Divider()
            }
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
                        Task { await model.refreshShown() }
                    } label: {
                        Label("Refresh", systemImage: "arrow.clockwise")
                    }
                    .help("Refresh")
                }
            }
            // A request that only shows its section is answered by showing it.
            .onChange(of: model.requested, initial: true) { _, requested in
                guard let requested, Self.answers(requested) != nil else { return }
                model.didHandle(requested)
            }
            .navigationTitle(onboarding ? "Keryx" : model.selection.title)
            .navigationSubtitle(onboarding ? "" : subtitle)
        }
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
            case .voice: VoiceView()
            case .models: ModelsView()
            case .phone: PhoneView()
            case .integrations: IntegrationsView()
            case .plugins: PluginsView()
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

/// A setting's save, refused or locally invalid, that landed with no row left to show it on
/// (`AppModel.settingSaveFailure`): the same sentence-plus-Show-Details shape as everywhere
/// else, over the whole window, with its own dismiss — never blocking, since nothing here
/// needs an answer.
struct SettingSaveFailureBanner: View {
    @Environment(AppModel.self) private var model
    let failure: StreamFailure

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Text(failure.message)
                .problemStyle()
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            if let detail = failure.detail {
                DetailsDisclosure(detail)
            }
            Spacer(minLength: 8)
            Button {
                model.dismissSettingSaveFailure()
            } label: {
                Image(systemName: "xmark")
            }
            .buttonStyle(.borderless)
            .accessibilityLabel("Dismiss")
        }
        .padding(10)
        .background(Color.red.opacity(0.08))
    }
}
