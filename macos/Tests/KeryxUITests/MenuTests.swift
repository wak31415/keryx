import Testing
@testable import KeryxKit
@testable import KeryxUI

@MainActor @Suite struct MenuTests {
    @Test func sessionItemsAreNumberedCapitalisedAndShort() {
        var task = PreviewData.tasks.tasks[1]
        task.id = 41
        task.description = "track down why the approval hook times out on a large diff"
        let title = MenuContent.title(task)
        #expect(title.hasPrefix("#41 Track down"))
        #expect(title.count == MenuContent.titleLimit)
        #expect(title.hasSuffix("…"))
    }

    @Test func theMenuIsInTheCatalog() {
        #expect(ScreenCatalog.entries().contains { $0.id == "menu" })
    }
}
