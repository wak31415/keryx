import KeryxKit
import KeryxUI
import SwiftUI

extension ScreenCatalog {
    /// The Overview screen: running with a setup problem, not set up at all, a status that
    /// failed, the approvals sheet, and the whole window around it.
    @MainActor static var overviewEntries: [Entry] {
        let screen = CGSize(width: 720, height: 880)
        return [
            Entry("overview-running", size: screen) {
                OverviewView().environment(AppModel.preview(client: runningClient))
            },
            Entry("overview-needs-setup", size: CGSize(width: 720, height: 900)) {
                OverviewView().environment(AppModel.preview(client: needsSetupClient))
            },
            Entry("overview-approvals-off", size: CGSize(width: 720, height: 760)) {
                OverviewView().environment(AppModel.preview(client: approvalsOffClient))
            },
            Entry("overview-error", size: CGSize(width: 720, height: 420)) {
                OverviewView().environment(AppModel.preview(client: PreviewClient(
                    status: .failure(.timedOut(command: "keryx status")))))
            },
            Entry("overview-approvals-sheet", size: CGSize(width: 700, height: 440)) {
                ApprovalsSheet().environment(AppModel.preview(client: runningClient))
            },
            Entry("window-overview", size: windowSize) {
                RootView().environment(AppModel.preview(client: runningClient))
            },
        ]
    }

    /// Everything set up, and the approval calls turned off.
    private static var approvalsOffClient: PreviewClient {
        var status = PreviewData.status
        status.approvals.escalationEnabled = false
        var doctor = PreviewData.doctor
        doctor.ok = true
        for index in doctor.checks.indices {
            doctor.checks[index].ok = true
            doctor.checks[index].state = "ok"
        }
        var approvals = brokerApprovals
        approvals.escalationEnabled = false
        return PreviewClient(status: .success(status), doctor: .success(doctor), approvals: .success(approvals))
    }

    /// The fixtures, with an approval trail in the events the broker actually writes.
    private static var runningClient: PreviewClient {
        PreviewClient(approvals: .success(brokerApprovals))
    }

    /// Three prompts: one answered on the call, one at the keyboard before the call, and
    /// one the owner chose to leave on screen. `what` is what `keryx approvals --json`
    /// carries: the request's summary.
    static var brokerApprovals: ApprovalsReport {
        var report = PreviewData.approvals
        let template = report.entries[0]
        let push = "Claude wants to run: git push origin main, in keryx"
        let alias = "Claude is asking: rename the module or keep the old name as an alias?"
        let clean = "Claude wants to run: rm -rf build/, in keryx-macos"
        let lines: [(String, String, Int?, String)] = [
            ("2026-10-05T08:58:30.104211+00:00", "started", nil, ""),
            ("2026-10-05T09:07:03.402114+00:00", "raised", 17, push),
            ("2026-10-05T09:12:03.512907+00:00", "escalated", 17, push),
            ("2026-10-05T09:12:41.118310+00:00", "armed", 17, push),
            ("2026-10-05T09:12:55.730021+00:00", "settled", 17, push),
            ("2026-10-05T10:38:12.004118+00:00", "raised", 18, alias),
            ("2026-10-05T10:41:30.381442+00:00", "resolve_fallback", 18, alias),
            ("2026-10-05T10:41:30.382017+00:00", "settled", 18, alias),
            ("2026-10-05T11:39:02.110934+00:00", "raised", 19, clean),
            ("2026-10-05T11:44:02.207731+00:00", "escalated", 19, clean),
            ("2026-10-05T11:44:40.990275+00:00", "keypad_unrecognised", 19, clean),
            ("2026-10-05T11:44:52.415560+00:00", "settled", 19, clean),
        ]
        // As `audit_log.escalation_counts` reckons them: the alias question settled before
        // the call; nothing was answered at the keyboard after one.
        report.cancelledBeforeCall = 1
        report.answeredAfterCall = 0
        report.entries = lines.map { ts, event, request, what in
            var entry = template
            entry.ts = ts
            entry.event = event
            entry.requestId = request.map { .number(Double($0)) }
            entry.what = what
            return entry
        }
        return report
    }

    /// A Mac where Keryx runs from a checkout but nothing else is done: no service, no
    /// phone, no memory yet.
    private static var needsSetupClient: PreviewClient {
        var status = PreviewData.status
        status.service.installed = false
        status.service.active = false
        status.health.reachable = false
        status.health.liveSessions = nil
        status.version.running = nil
        status.tasks.running = 0
        status.tasks.unreported = 0

        var doctor = PreviewData.doctor
        for index in doctor.checks.indices where doctor.checks[index].section == "voice" {
            doctor.checks[index].state = "missing"
            doctor.checks[index].ok = false
            doctor.checks[index].unset = true
            doctor.checks[index].detail = "not set"
        }

        var memory = PreviewData.memory
        memory.exists = false
        memory.text = nil

        var approvals = PreviewData.approvals
        approvals.entries = []

        return PreviewClient(status: .success(status), doctor: .success(doctor),
                             approvals: .success(approvals), memory: .success(memory))
    }
}
