import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

@MainActor @Suite struct MenuTests {
    @Test func sessionItemsAreCapitalisedShortAndUnnumbered() {
        var task = PreviewData.tasks.tasks[1]
        task.id = 41
        task.description = "track down why the approval hook times out on a large diff"
        let title = MenuContent.title(task)
        #expect(title.hasPrefix("Track down"))
        #expect(!title.contains("#41"))
        #expect(title.count <= MenuContent.titleLimit)
        #expect(title.hasSuffix("…"))
    }

    @Test func theFirstLineIsTheStateAndTheCountsFollow() {
        var status = PreviewData.status
        status.tasks.running = 1
        status.tasks.unreported = 2
        let state = menuState(location: Loadable(value: .fixture, updatedAt: nil), status: status, statusError: nil)
        #expect(state == .attention("2 results not yet told"))
        #expect(MenuContent.stateLine(state, status: status) == "Keryx is running")
        #expect(MenuContent.counts(status) == ["1 session running", "2 results not yet told"])

        status.restart.state = "failed"
        let failed = menuState(location: Loadable(value: .fixture, updatedAt: nil), status: status, statusError: nil)
        #expect(MenuContent.stateLine(failed, status: status) == "Last restart failed")
        #expect(MenuContent.stateLine(.stopped, status: status) == "Keryx is stopped")
    }

    @Test func theMenuAndTheSettingsWindowAreInTheCatalog() {
        let ids = Set(ScreenCatalog.entries().map(\.id))
        for id in ["menu", "menu-not-installed", "settings", "settings-login-needs-approval"] {
            #expect(ids.contains(id), "missing \(id)")
        }
    }
}
