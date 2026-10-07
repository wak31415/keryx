import Foundation
import KeryxKit
import SwiftUI

extension ScreenCatalog {
    /// The Sessions screen: a running session open, a finished one with a follow-up, a
    /// failed one, one not yet told, nothing selected, no sessions at all, a list that
    /// failed to load, and the whole window around it.
    @MainActor static var sessionsEntries: [Entry] {
        let screen = CGSize(width: 960, height: 680)
        return [
            Entry("sessions-running", size: screen) {
                SessionsView().environment(sessionsModel(selecting: 41, detail: runningDetail))
            },
            Entry("sessions-done-followup", size: CGSize(width: 960, height: 900)) {
                SessionsView().environment(sessionsModel(selecting: 40, detail: PreviewData.taskDetail))
            },
            Entry("sessions-failed", size: screen) {
                SessionsView().environment(sessionsModel(selecting: 38, detail: failedDetail))
            },
            Entry("sessions-untold", size: screen) {
                SessionsView().environment(sessionsModel(selecting: 43, detail: untoldDetail))
            },
            Entry("sessions-no-selection", size: screen) {
                SessionsView().environment(sessionsModel(selecting: nil, detail: PreviewData.taskDetail))
            },
            Entry("sessions-empty", size: CGSize(width: 720, height: 480)) {
                SessionsView().environment(AppModel.preview(client: PreviewClient(tasks: .success([]))))
            },
            Entry("sessions-filtered-empty", size: CGSize(width: 720, height: 480)) {
                SessionsView().environment(filteredEmptyModel)
            },
            Entry("sessions-error", size: CGSize(width: 720, height: 480)) {
                SessionsView().environment(AppModel.preview(client: PreviewClient(
                    tasks: .failure(.failed(command: "keryx tasks list", exitCode: 1,
                                            message: "database is locked")))))
            },
            Entry("window-sessions", size: windowSize) {
                RootView().environment(sessionsModel(selecting: 41, detail: runningDetail, window: true))
            },
        ]
    }

    /// The preview model on the Sessions screen with `id` open, its detail answering `detail`.
    @MainActor private static func sessionsModel(selecting id: Int?, detail: TaskDetail,
                                                 window: Bool = false) -> AppModel {
        let model = AppModel.preview(client: PreviewClient(tasks: .success(sessionTasks), task: .success(detail)))
        model.selectedTaskID = id
        if window { model.selection = .sessions }
        return model
    }

    /// Showing only failed sessions, with housekeeping on, when none failed.
    @MainActor private static var filteredEmptyModel: AppModel {
        let model = AppModel.preview(client: PreviewClient(tasks: .success(PreviewData.tasks.tasks.filter {
            $0.status != .failed
        })))
        model.statusFilter = .failed
        model.includeHousekeeping = true
        return model
    }

    /// The fixture's sessions, and one more on top: a Codex session with a long ask, not
    /// yet told.
    private static var sessionTasks: [TaskItem] {
        [untoldDetail.task] + PreviewData.tasks.tasks
    }

    private static func task(_ id: Int) -> TaskItem {
        PreviewData.tasks.tasks.first { $0.id == id }!
    }

    /// Session 41, still running, its earlier steps cut from the log.
    private static var runningDetail: TaskDetail {
        let steps = [
            "read scripts/claude_hooks/keryx_approval.py",
            "read src/keryx/approvals/broker.py",
            "read src/keryx/approvals/policy.py",
            "ran uv run pytest tests/approvals -q",
            "wrote a 4 MB diff fixture to reproduce the timeout",
            "ran the hook against the fixture: it blocks for 31 s",
            "read the socket reader in broker.py",
            "found it: the reader waits for EOF before parsing, and the CLI keeps the pipe open",
            "ran git log -p on the reader",
            "wrote tests/approvals/test_large_request.py",
            "ran uv run pytest tests/approvals/test_large_request.py -q — fails as expected",
            "edited src/keryx/approvals/broker.py to read a length-prefixed frame",
        ]
        let log = steps.enumerated().map { index, text in
            SessionLogEntry(id: index, time: String(format: "08:%02d:%02d", 16 + index * 2, (index * 17) % 60),
                            kind: .progress, text: text)
        }
        return TaskDetail(task: task(41), report: nil, reportTruncated: false, log: log,
                          logTruncated: true, resumable: true)
    }

    /// Session 38, which failed, its report cut short.
    private static var failedDetail: TaskDetail {
        let steps = [
            "read ops/launchd/dev.keryx.llm.plist",
            "read src/keryx/doctor.py",
            "wrote a check for the llama-server unit",
            "ran uv run pytest tests/test_doctor.py -q",
            "launchctl refused the plist: no Label key",
        ]
        let log = steps.enumerated().map { index, text in
            SessionLogEntry(id: index, time: String(format: "14:5%d:1%d", index, index), kind: .progress, text: text)
        }
        let report = "The check is written, but the unit it checks cannot load: "
            + "`ops/launchd/dev.keryx.llm.plist` has no `Label` key, so `launchctl bootstrap` "
            + "refuses it. I stopped there rather than guess at the label."
        return TaskDetail(task: task(38), report: report, reportTruncated: true, log: log,
                          logTruncated: false, resumable: true)
    }

    /// A Codex session with a long ask, finished and not yet told.
    private static var untoldDetail: TaskDetail {
        var session = task(39)
        session.id = 43
        session.agent = "codex"
        session.description = "rework the first call so that someone ringing for the very first time hears "
            + "the short introduction before the PIN prompt, and keep the memory skeleton exactly as it is"
        session.createdAt = Date(timeIntervalSinceNow: -50 * 60)
        session.startedAt = session.createdAt.addingTimeInterval(4)
        session.finishedAt = session.createdAt.addingTimeInterval(22 * 60)
        session.summary = "The first call now opens with the introduction, then asks for the PIN; "
            + "the memory skeleton is untouched."
        let steps = [
            "read src/keryx/prompts/first_call.md",
            "read src/keryx/continuity/memory.py",
            "edited src/keryx/prompts/first_call.md",
            "ran uv run pytest tests/prompts -q",
        ]
        let log = steps.enumerated().map { SessionLogEntry(id: $0, time: nil, kind: .progress, text: $1) }
        let report = "## What I did\n\nMoved the introduction above the PIN prompt in `first_call.md`.\n\n"
            + "- No change to `memory_skeleton`\n- Tests pass"
        return TaskDetail(task: session, report: report, reportTruncated: false, log: log,
                          logTruncated: false, resumable: true)
    }
}
