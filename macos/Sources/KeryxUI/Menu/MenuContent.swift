import AppKit
import KeryxKit
import SwiftUI

/// What the menu's items do outside KeryxUI: the app opens its windows and quits.
public struct MenuActions {
    public var openWindow: () -> Void
    public var openSession: (Int) -> Void
    public var quit: () -> Void
    /// Opens the setup wizard, every frame prefilled (review).
    public var openSetup: () -> Void

    public init(openWindow: @escaping () -> Void, openSession: @escaping (Int) -> Void,
                quit: @escaping () -> Void, openSetup: @escaping () -> Void = {}) {
        self.openWindow = openWindow
        self.openSession = openSession
        self.quit = quit
        self.openSetup = openSetup
    }
}

/// The menu bar menu, for `.menuBarExtraStyle(.menu)`: only text, buttons, toggles,
/// dividers and sections, which is all an `NSMenu` can show.
public struct MenuContent: View {
    /// How many sessions the menu lists, and how long each item may be.
    static let sessionLimit = 3
    static let titleLimit = 40

    @Environment(AppModel.self) private var model
    private let actions: MenuActions

    public init(actions: MenuActions) {
        self.actions = actions
    }

    public var body: some View {
        Text(header)
        ForEach(Self.counts(model.status.value), id: \.self) { Text($0) }
        if let localModel { Text(localModel) }
        if let download { Text(download) }

        if !sessions.isEmpty {
            Section {
                ForEach(sessions) { task in
                    Button(Self.title(task)) { actions.openSession(task.id) }
                }
            } header: {
                // A menu's section header: small and bold, never mistaken for an item.
                Text("Sessions").font(.caption.weight(.semibold))
            }
        }

        Divider()

        Button("Open Keryx") { actions.openWindow() }
            .keyboardShortcut("o")
        Button("Run Setup Wizard…") { actions.openSetup() }
        Button("Restart Keryx…") { MenuAlerts.restart(model: model) }
            .disabled(model.client == nil || model.status.value?.service.installed != true)
        Toggle("Pause Approval Calls", isOn: paused)
            .disabled(model.status.value == nil)

        Divider()

        SettingsLink { Text("Settings…") }
            .keyboardShortcut(",")
        Button("Quit Keryx") { actions.quit() }
            .keyboardShortcut("q")
    }

    /// "Keryx is running — Lyra": the state first, whatever else there is to say.
    private var header: String {
        let state = Self.stateLine(model.menuState, status: model.status.value)
        guard let assistant = Wording.assistant(model.status.value) else { return state }
        return "\(state) — \(assistant)"
    }

    /// Whether Keryx is running, stopped or missing. Results waiting to be told are a count
    /// under it, not the state itself.
    static func stateLine(_ state: MenuState, status: StatusReport?) -> String {
        if case .attention = state, let status, status.storage.ok, status.restart.state != "failed",
           status.health.reachable || status.service.active {
            return MenuState.idle.headline
        }
        return state.headline
    }

    /// The lines under the state: "1 session running", "2 results not yet told".
    static func counts(_ status: StatusReport?) -> [String] {
        guard let tasks = status?.tasks, tasks.ok else { return [] }
        var lines: [String] = []
        if tasks.running > 0 { lines.append(tasks.running == 1 ? "1 session running" : "\(tasks.running) sessions running") }
        if tasks.unreported > 0 {
            lines.append(tasks.unreported == 1 ? "1 result not yet told" : "\(tasks.unreported) results not yet told")
        }
        return lines
    }

    /// Sessions running, then those finished but not yet told, newest first.
    private var sessions: [TaskItem] {
        let tasks = model.tasks.value ?? []
        let live = tasks.filter { $0.status == .running }
        let untold = tasks.filter { $0.status != .running && $0.told == false }
        return Array((live + untold).prefix(Self.sessionLimit))
    }

    /// "Track down why the approval hook…".
    static func title(_ task: TaskItem) -> String {
        Wording.truncated(Wording.capitalised(task.description), to: titleLimit)
    }

    /// "Local model running", when a local agent is configured; which model is the
    /// window's to say.
    private var localModel: String? {
        guard let report = model.models.value, report.servers.localAgent.model != nil,
              report.servers.localAgent.enabled else { return nil }
        return report.servers.llm.reachable ? "Local model running" : "Local model stopped"
    }

    /// "Downloading Gemma 4 12B — 42%" while a download runs, "Stopping the download…"
    /// while one stops.
    private var download: String? {
        guard let current = model.downloads.current else { return nil }
        let title = model.models.value?.models.first { $0.name == current.name }?.title ?? current.name
        switch current.phase {
        case let .running(done, total):
            guard total > 0 else { return "Downloading \(title)" }
            let percent = Int((Double(done) / Double(total) * 100).rounded(.down))
            return "Downloading \(title) — \(percent)%"
        case .cancelling:
            return "Stopping the download…"
        case .failed, .finished:
            return nil
        }
    }

    /// Pausing asks first, as the Overview's switch does; turning calls back on does not.
    private var paused: Binding<Bool> {
        Binding {
            !(model.status.value?.approvals.escalationEnabled ?? true)
        } set: { pause in
            if pause && !MenuAlerts.confirmPause() { return }
            Task {
                if let error = await model.setEscalation(enabled: !pause) {
                    MenuAlerts.show(error)
                }
            }
        }
    }
}

/// The menu's dialogs, where there is no window to hold one: the restart's confirmation (and,
/// when calls are in progress, a second one), pausing the approval calls, and errors, as
/// app-modal alerts.
@MainActor enum MenuAlerts {
    static func restart(model: AppModel) {
        NSApp.activate()
        let confirm = NSAlert()
        confirm.messageText = "Restart Keryx?"
        confirm.informativeText = OverviewWording.restartMessage
        confirm.addButton(withTitle: "Restart")
        confirm.addButton(withTitle: "Cancel")
        guard confirm.runModal() == .alertFirstButtonReturn else { return }
        Task {
            switch await model.restart(force: false) {
            case nil:
                return
            case let .liveCalls(count):
                let anyway = NSAlert()
                anyway.messageText = count == 1 ? "1 call is in progress." : "\(count) calls are in progress."
                anyway.informativeText = count == 1 ? "Restarting ends it." : "Restarting ends them."
                anyway.addButton(withTitle: "Restart Anyway").hasDestructiveAction = true
                anyway.addButton(withTitle: "Cancel")
                guard anyway.runModal() == .alertFirstButtonReturn else { return }
                if let error = await model.restart(force: true) { show(error) }
            case let error?:
                show(error)
            }
        }
    }

    /// Whether the owner really means to stop the approval calls.
    static func confirmPause() -> Bool {
        NSApp.activate()
        let alert = NSAlert()
        alert.messageText = "Turn Off Approval Calls?"
        alert.informativeText = ApprovalCallsWording.offWarning
        alert.addButton(withTitle: "Turn Off")
        alert.addButton(withTitle: "Cancel")
        return alert.runModal() == .alertFirstButtonReturn
    }

    static func show(_ error: KeryxError) {
        NSApp.activate()
        let alert = NSAlert()
        alert.messageText = error.title
        alert.informativeText = error.message
        alert.runModal()
    }
}
