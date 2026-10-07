import AppKit
import KeryxKit
import SwiftUI

/// What the menu's items do outside KeryxUI: the app opens its window and quits.
public struct MenuActions {
    public var openWindow: () -> Void
    public var openSession: (Int) -> Void
    public var quit: () -> Void

    public init(openWindow: @escaping () -> Void, openSession: @escaping (Int) -> Void,
                quit: @escaping () -> Void) {
        self.openWindow = openWindow
        self.openSession = openSession
        self.quit = quit
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

        if let running, running > 0 {
            Text(running == 1 ? "1 session running" : "\(running) sessions running")
        }
        if !sessions.isEmpty {
            Section("Sessions") {
                ForEach(sessions) { task in
                    Button(Self.title(task)) { actions.openSession(task.id) }
                }
            }
        }
        if let localModel { Text(localModel) }
        if let download { Text(download) }

        Divider()

        Button("Open Keryx") { actions.openWindow() }
            .keyboardShortcut("o")
        Button("Restart Keryx…") { MenuAlerts.restart(model: model) }
            .disabled(model.client == nil)
        Toggle("Pause Approval Calls", isOn: paused)
            .disabled(model.status.value == nil)

        Divider()

        SettingsLink { Text("Settings…") }
            .keyboardShortcut(",")
        Button("Quit Keryx") { actions.quit() }
            .keyboardShortcut("q")
    }

    /// "Keryx is running — Lyra".
    private var header: String {
        let headline = model.menuState.headline
        guard let assistant = Wording.assistant(model.status.value) else { return headline }
        return "\(headline) — \(assistant)"
    }

    private var running: Int? { model.status.value?.tasks.running }

    /// Sessions running, then those finished but not yet told, newest first.
    private var sessions: [TaskItem] {
        let tasks = model.tasks.value ?? []
        let live = tasks.filter { $0.status == .running }
        let untold = tasks.filter { $0.status != .running && $0.told == false }
        return Array((live + untold).prefix(Self.sessionLimit))
    }

    /// "#41 Track down why the approval hook…".
    static func title(_ task: TaskItem) -> String {
        Wording.truncated("#\(task.id) " + Wording.capitalised(task.description), to: titleLimit)
    }

    /// "Local model: Qwen3 Coder 30B — running", when a local agent is configured.
    private var localModel: String? {
        guard let report = model.models.value, let name = report.servers.localAgent.model,
              report.servers.localAgent.enabled else { return nil }
        let title = report.models.first { $0.name == name }?.title ?? name
        return "Local model: \(title) — \(report.servers.llm.reachable ? "running" : "not running")"
    }

    /// "Downloading Gemma 3 12B — 42%", while a download runs.
    private var download: String? {
        guard let current = model.downloads.current,
              case let .running(done, total) = current.phase else { return nil }
        let title = model.models.value?.models.first { $0.name == current.name }?.title ?? current.name
        guard total > 0 else { return "Downloading \(title)" }
        let percent = Int((Double(done) / Double(total) * 100).rounded(.down))
        return "Downloading \(title) — \(percent)%"
    }

    private var paused: Binding<Bool> {
        Binding {
            !(model.status.value?.approvals.escalationEnabled ?? true)
        } set: { pause in
            Task {
                if let error = await model.setEscalation(enabled: !pause) {
                    MenuAlerts.show(error)
                }
            }
        }
    }
}

/// The menu's dialogs, where there is no window to hold one: the restart's
/// confirmation (and, when calls are in progress, a second one), and errors, as app-modal alerts.
@MainActor enum MenuAlerts {
    static func restart(model: AppModel) {
        NSApp.activate()
        let confirm = NSAlert()
        confirm.messageText = "Restart Keryx?"
        confirm.informativeText = "Keryx stops, starts again, and calls you when it's back."
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

    static func show(_ error: KeryxError) {
        NSApp.activate()
        let alert = NSAlert()
        alert.messageText = error.title
        alert.informativeText = error.message
        alert.runModal()
    }
}
