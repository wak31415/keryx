import KeryxKit
import SwiftUI

/// The Sessions screen, laid out like Mail: the agent sessions down the left, newest first,
/// and the chosen one's timeline on the right.
public struct SessionsView: View {
    @Environment(AppModel.self) private var model

    public init() {}

    /// How often a running session's steps are read again while it is on screen.
    static let livePoll: Duration = .seconds(10)

    public var body: some View {
        @Bindable var model = model
        StateView(model.tasks, retry: { Task { await model.refresh(.tasks) } }) { tasks in
            let sessions = SessionWording.visible(tasks, filter: model.statusFilter,
                                                  housekeeping: model.includeHousekeeping)
            if sessions.isEmpty {
                empty(anyAtAll: !SessionWording.visible(tasks, filter: nil,
                                                       housekeeping: model.includeHousekeeping).isEmpty)
            } else {
                HSplitView {
                    SessionList(sessions: sessions)
                        .frame(minWidth: 260, idealWidth: 320, maxWidth: 360)
                    timeline
                        .frame(minWidth: 400, maxWidth: .infinity, maxHeight: .infinity)
                }
            }
        }
        .toolbar {
            ToolbarItemGroup {
                Picker("Show", selection: $model.statusFilter) {
                    ForEach(SessionWording.filters, id: \.self) { filter in
                        Text(SessionWording.filterTitle(filter)).tag(filter)
                    }
                }
                .pickerStyle(.menu)
                .help("Show sessions by status")

                Toggle(isOn: $model.includeHousekeeping) {
                    Label("Show Housekeeping", systemImage: "wrench.and.screwdriver")
                }
                .help("Show Housekeeping: the sessions Keryx starts for itself, such as updating the memory")
            }
        }
        .task {
            await model.refresh(.tasks)
            await model.refresh(.usage)
        }
        .onChange(of: model.includeHousekeeping) {
            Task { await model.refresh(.tasks) }
        }
        .onChange(of: model.statusFilter) {
            Task { await model.refresh(.tasks) }
        }
        .onChange(of: model.selectedTaskID, initial: true) { _, id in
            guard let id else { return }
            reveal(id, refreshing: true)
            Task { await model.loadTask(id) }
        }
        .onChange(of: model.tasks.updatedAt) {
            // A refresh of the list brings the open session's steps up to date with it.
            guard let id = model.selectedTaskID else { return }
            reveal(id, refreshing: false)
            Task { await model.loadTask(id) }
        }
        .task(id: liveSession) {
            // While the open session is still going, read it (and the list) again now and then.
            guard liveSession != nil else { return }
            while !Task.isCancelled {
                do { try await Task.sleep(for: Self.livePoll) } catch { return }
                await model.refresh(.tasks)
            }
        }
    }

    // MARK: - Detail

    @ViewBuilder private var timeline: some View {
        if let id = model.selectedTaskID {
            let detail = model.taskDetail[id]
            if let task = current(id) {
                SessionTimeline(task: task, detail: detail, assistant: assistant)
                    .id(id)
            } else {
                StateView(detail ?? Loadable(isLoading: true),
                          retry: { Task { await model.loadTask(id) } }) { value in
                    SessionTimeline(task: value.task, detail: detail, assistant: assistant)
                }
                .id(id)
            }
        } else {
            ContentUnavailableView("Select a Session", systemImage: "bubble.left.and.text.bubble.right")
        }
    }

    /// The session as most recently read: from the list or from its own detail, whichever
    /// loaded last.
    private func current(_ id: Int) -> TaskItem? {
        let listed = model.tasks.value?.first { $0.id == id }
        guard let detail = model.taskDetail[id], let fromDetail = detail.value?.task else { return listed }
        guard let listed, let listedAt = model.tasks.updatedAt else { return fromDetail }
        return (detail.updatedAt ?? .distantPast) >= listedAt ? fromDetail : listed
    }

    /// The open session's id while it is queued or running.
    private var liveSession: Int? {
        guard let id = model.selectedTaskID, let task = current(id),
              task.status == .running || task.status == .queued else { return nil }
        return id
    }

    private var assistant: String {
        Wording.assistant(model.status.value) ?? "your assistant"
    }

    // MARK: - Empty

    @ViewBuilder private func empty(anyAtAll: Bool) -> some View {
        if anyAtAll, let filter = model.statusFilter {
            ContentUnavailableView {
                Label("No \(SessionWording.filterTitle(filter)) Sessions",
                      systemImage: "bubble.left.and.text.bubble.right")
            } description: {
                Text(SessionWording.noneMatching(filter))
            } actions: {
                Button("Show All") { model.statusFilter = nil }
            }
        } else {
            ContentUnavailableView(
                "No Sessions", systemImage: "bubble.left.and.text.bubble.right",
                description: Text("When you ask \(assistant) for something on a call, the agent session appears here."))
        }
    }

    // MARK: - Selection

    /// Makes sure the list can show session `id` — opened from the menu, it may be one the
    /// filter hides, or one newer than the list (which `refreshing` reads again).
    private func reveal(_ id: Int, refreshing: Bool) {
        guard let tasks = model.tasks.value else { return }
        guard let task = tasks.first(where: { $0.id == id }) else {
            if refreshing { Task { await model.refresh(.tasks) } }
            return
        }
        if task.internal && !model.includeHousekeeping { model.includeHousekeeping = true }
        if !SessionWording.matches(task.status, filter: model.statusFilter) { model.statusFilter = nil }
    }
}

/// The source list, with the 30 days' totals under it.
private struct SessionList: View {
    let sessions: [TaskItem]

    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        ScrollViewReader { proxy in
            List(sessions, selection: $model.selectedTaskID) { task in
                SessionRow(task: task)
                    .tag(task.id)
            }
            .listStyle(.inset)
            .onChange(of: model.selectedTaskID, initial: true) { _, id in
                guard let id else { return }
                // After the list has laid out, so a session opened from the menu is in view.
                Task { @MainActor in proxy.scrollTo(id) }
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if let usage = model.usage.value {
                VStack(spacing: 0) {
                    Divider()
                    Text(SessionWording.footer(usage, housekeeping: model.includeHousekeeping))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                        .lineLimit(1)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 6)
                        .padding(.horizontal, 12)
                }
                .background(.bar)
            }
        }
    }
}
