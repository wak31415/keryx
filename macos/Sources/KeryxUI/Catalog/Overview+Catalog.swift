import KeryxKit
import SwiftUI

extension ScreenCatalog {
    /// The Overview screen: running with a setup problem, not set up at all, a status that
    /// failed, the approvals sheet, and the whole window around it.
    @MainActor static var overviewEntries: [Entry] {
        let screen = CGSize(width: 720, height: 880)
        return [
            Entry("overview-running", size: screen) {
                OverviewView().environment(AppModel.preview())
            },
            Entry("overview-needs-setup", size: CGSize(width: 720, height: 900)) {
                OverviewView().environment(AppModel.preview(client: needsSetupClient))
            },
            Entry("overview-error", size: CGSize(width: 720, height: 420)) {
                OverviewView().environment(AppModel.preview(client: PreviewClient(
                    status: .failure(.timedOut(command: "keryx status")))))
            },
            Entry("overview-approvals-sheet", size: CGSize(width: 700, height: 440)) {
                ApprovalsSheet().environment(AppModel.preview())
            },
            Entry("window-overview", size: windowSize) {
                RootView().environment(AppModel.preview())
            },
        ]
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
