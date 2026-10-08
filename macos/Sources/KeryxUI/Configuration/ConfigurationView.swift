import KeryxKit
import SwiftUI

/// Every setting `keryx config list` shows, on the tabs it names (General, Voice, Agents …),
/// as a Settings window lays them out: a bar of tabs along the top, and under it the chosen
/// tab's key settings, by group, with the rest under a collapsed Advanced disclosure that
/// remembers, tab by tab, whether it was opened. A search looks through every tab, the
/// advanced settings too, and shows what it finds under the tabs' names; a setting a screen
/// owns (Voice's, Local Models') is found as a link to that screen, its one editor. Changes go through
/// the CLI (`AppModel.set`, `setSecret`, `unset`); once one lands, a banner at the top
/// offers the restart that loads it.
public struct ConfigurationView: View {
    @Environment(AppModel.self) private var model
    @State private var search: String
    /// The tab chosen; nil until one is, which shows the first.
    @State private var selection: String?
    /// The tabs whose Advanced disclosure is open.
    @State private var advancedOpen: Set<String>
    /// Errors to show under rows when the screen first appears (the catalog's write error).
    private let initialErrors: [String: KeryxError]
    /// The PIN sheet, asked for by another window (`AppRequest.setPin`); through `PinFlow`,
    /// so a working PIN is asked about before it is replaced.
    @State private var pinRequested = false
    /// Claude's or Codex's sign-in sheet, from the Agents tab or asked for by another window
    /// (`AppRequest.signIn`).
    @State private var signingIn: SignInProvider?
    /// The setting asked for by another window (`AppRequest.setting`): scrolled to, and
    /// marked a moment.
    @State private var focus: String?

    public init() {
        self.init(search: "")
    }

    package init(search: String, tab: String? = nil, advancedOpen: Set<String> = [],
                 errors: [String: KeryxError] = [:]) {
        _search = State(initialValue: search)
        _selection = State(initialValue: tab)
        _advancedOpen = State(initialValue: advancedOpen)
        initialErrors = errors
    }

    public var body: some View {
        StateView(model.config, section: "Configuration", retry: { Task { await model.refresh(.config) } }) { rows in
            let tabs = ConfigWording.tabs(rows)
            let current = ConfigWording.selected(selection, in: tabs)
            let titles = Wording.titles(rows)
            VStack(spacing: 0) {
                ConfigTabBar(tabs: tabs, selection: searching ? nil : current?.id, select: { choose($0) })
                Divider()
                if searching {
                    results(rows, titles: titles)
                } else if let current {
                    tabForm(current, titles: titles)
                        .id(current.id)
                }
            }
            // The tab bar on the very background the forms below it have, whatever the
            // appearance: an empty grouped form is that background.
            .background {
                Form {}
                    .formStyle(.grouped)
                    .accessibilityHidden(true)
            }
        }
        .searchable(text: $search, prompt: "Search Settings")
        .task {
            await model.refresh(.config)
            // Whether the coding agents are signed in (the Agents tab's sign-ins).
            if model.doctor.value == nil { await model.refresh(.doctor) }
        }
        .onAppear { answer(model.requested) }
        .onChange(of: model.requested) { answer(model.requested) }
        // A setting asked for before the settings were read: answered once they are.
        .onChange(of: model.config.value == nil) { answer(model.requested) }
        .pinSheet(isPresented: $pinRequested)
        .sheet(item: $signingIn, onDismiss: { Task { await model.refresh(.doctor, .setupPlan) } }) { provider in
            SignInSheet(kind: provider)
        }
    }

    /// Does what another window asked of Configuration (`AppModel.requested`): a tab, or
    /// the PIN sheet on the PIN's tab.
    private func answer(_ request: AppRequest?) {
        guard let request else { return }
        // A setting a screen owns is changed there, not here.
        if case let .setting(key) = request, let screen = ConfigWording.screen(of: key, in: model.config.value) {
            model.request(.show(screen))
            return
        }
        guard let answer = Self.answer(request, config: model.config.value) else { return }
        choose(answer.tab)
        if let key = answer.focus {
            if model.config.value?.first(where: { $0.key == key })?.advanced == true { advancedOpen.insert(answer.tab) }
            focus = key
        }
        switch answer.sheet {
        case .pin?: pinRequested = true
        case let .signIn(provider)?: signingIn = provider
        case nil: break
        }
        model.didHandle(request)
    }

    /// What this screen does for `request` (`RequestAnswer`), with the settings as read.
    static func answer(_ request: AppRequest,
                       config: [ConfigRow]?) -> (tab: String, sheet: ConfigurationSheet?, focus: String?)? {
        ConfigWording.answer(request, pinTab: config?.first(where: ConfigWording.isPIN)?.tab, config: config)
    }

    // MARK: - A tab

    private func tabForm(_ tab: ConfigWording.Tab, titles: [String: String]) -> some View {
        ScrollViewReader { proxy in
        Form {
            if model.configNeedsRestart {
                RestartBanner()
            }
            if tab.id == RequestAnswer.agentsTab {
                AgentSignInSection { signingIn = $0 }
            }
            ForEach(tab.groups) { group in
                Section {
                    rows(group.rows, titles: titles)
                } header: {
                    if let title = ConfigWording.sectionTitle(group, on: tab) {
                        Text(title)
                    }
                }
            }
            if !tab.advanced.isEmpty {
                // Every advanced setting under the one Advanced disclosure, its groups as
                // headings inside it rather than sections of their own beside the tab's.
                Section {
                    DisclosureGroup(isExpanded: advancedBinding(tab.id)) {
                        ForEach(tab.advanced) { group in
                            if let title = ConfigWording.advancedSectionTitle(group, on: tab) {
                                Text(title)
                                    .font(.subheadline.weight(.semibold))
                                    .foregroundStyle(.secondary)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(.top, 6)
                            }
                            separatedRows(group.rows, titles: titles)
                        }
                    } label: {
                        LabeledContent("Advanced") {
                            Text(ConfigWording.advancedCount(tab))
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .formStyle(.grouped)
        .onChange(of: focus, initial: true) { _, key in
            guard let key else { return }
            Task {
                // Once the rows are laid out.
                await Task.yield()
                withAnimation { proxy.scrollTo(key, anchor: .center) }
                try? await Task.sleep(for: .seconds(3))
                if focus == key { withAnimation(.easeOut(duration: 0.6)) { focus = nil } }
            }
        }
        }
    }

    // MARK: - A search

    @ViewBuilder private func results(_ rows: [ConfigRow], titles: [String: String]) -> some View {
        let found = ConfigWording.results(rows, for: search)
        let elsewhere = ConfigWording.screenResults(rows, for: search)
        if found.isEmpty, elsewhere.isEmpty {
            ContentUnavailableView.search(text: search)
                .frame(maxHeight: .infinity)
        } else {
            Form {
                if model.configNeedsRestart {
                    RestartBanner()
                }
                ForEach(found) { tab in
                    if !tab.keyRows.isEmpty {
                        Section(ConfigWording.resultTitle(tab, advanced: false)) {
                            self.rows(tab.keyRows, titles: titles)
                        }
                    }
                    if !tab.advancedRows.isEmpty {
                        Section(ConfigWording.resultTitle(tab, advanced: true)) {
                            self.rows(tab.advancedRows, titles: titles)
                        }
                    }
                }
                ForEach(elsewhere) { result in
                    Section(result.title) {
                        ForEach(result.rows) { row in
                            ScreenLinkRow(row: row, link: result.link, titles: titles) {
                                model.request(.show(result.screen))
                            }
                        }
                    }
                }
            }
            .formStyle(.grouped)
        }
    }

    private func rows(_ rows: [ConfigRow], titles: [String: String]) -> some View {
        ForEach(rows) { row in
            rowView(row, titles: titles)
        }
    }

    /// Rows inside the Advanced disclosure, where the form draws no separators of its own:
    /// spaced and divided as the form above them is.
    private func separatedRows(_ rows: [ConfigRow], titles: [String: String]) -> some View {
        ForEach(Array(rows.enumerated()), id: \.element.id) { index, row in
            if index > 0 { Divider() }
            rowView(row, titles: titles)
                .padding(.vertical, 4)
        }
    }

    private func rowView(_ row: ConfigRow, titles: [String: String]) -> some View {
        ConfigRowView(row: row, titles: titles, error: initialErrors[row.key])
            .id(row.key)
            .background {
                RoundedRectangle(cornerRadius: 6)
                    .fill(Color.accentColor.opacity(focus == row.key ? 0.14 : 0))
                    .padding(.horizontal, -6)
                    .padding(.vertical, -2)
            }
    }

    // MARK: - State

    private var searching: Bool {
        !search.trimmingCharacters(in: .whitespaces).isEmpty
    }

    /// Shows a tab, leaving any search for it.
    private func choose(_ tab: String) {
        guard !tab.isEmpty else { return }
        selection = tab
        search = ""
    }

    private func advancedBinding(_ tab: String) -> Binding<Bool> {
        Binding {
            advancedOpen.contains(tab)
        } set: { isOpen in
            if isOpen { advancedOpen.insert(tab) } else { advancedOpen.remove(tab) }
        }
    }
}

/// A setting a screen owns, as a search finds it: its title and description, and in place
/// of an editor the link to the screen that has the one ("Change in Voice ›").
private struct ScreenLinkRow: View {
    let row: ConfigRow
    let link: String
    let titles: [String: String]
    let open: () -> Void

    var body: some View {
        LabeledContent {
            Button(link, action: open)
                .buttonStyle(.link)
        } label: {
            Text(row.displayTitle)
            if !row.description.isEmpty {
                Text(ConfigWording.described(ConfigDescriptions.text(row), titles: titles))
                    .lineLimit(2)
                    .truncationMode(.tail)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .help(ConfigWording.tooltip(row))
        .id(row.key)
    }
}
