import Foundation
import Testing
@testable import KeryxKit
@testable import KeryxUI

@Suite struct WordingTests {
    @Test func agentsHaveTheirOwnersNames() {
        #expect(Wording.agent("claude") == "Claude")
        #expect(Wording.agent("codex") == "Codex")
        #expect(Wording.agent("local") == "Local model")
        #expect(Wording.agent("gemini") == "gemini")
    }

    @Test func shortStatusIsLowerCase() {
        #expect(Wording.shortStatus(.idle) == "running")
        #expect(Wording.shortStatus(.working(2)) == "2 sessions running")
        #expect(Wording.shortStatus(.attention("Last restart failed")) == "last restart failed")
        #expect(Wording.shortStatus(.locating) == nil)
    }

    @Test func truncationEndsInAnEllipsis() {
        #expect(Wording.truncated("short", to: 40) == "short")
        let cut = Wording.truncated(String(repeating: "a", count: 50), to: 40)
        #expect(cut.count == 40)
        #expect(cut.hasSuffix("…"))
    }
}

@Suite struct ApprovalWordingTests {
    func entry(_ event: String, _ what: String) -> ApprovalEntry {
        ApprovalEntry(ts: "2026-10-05T09:12:03.123456+00:00", event: event, what: what, requestId: nil)
    }

    @Test func verdictsReadAsSentences() {
        #expect(ApprovalWording.describe(entry("answered_at_keyboard", "allow")) == ("Allowed at the keyboard", nil))
        #expect(ApprovalWording.describe(entry("answered_after_call", "deny")) == ("Denied on the call", nil))
    }

    @Test func commandsKeepTheirCase() {
        #expect(ApprovalWording.describe(entry("escalated", "rm -rf build/")) == ("Called you", "rm -rf build/"))
        #expect(ApprovalWording.describe(entry("cancelled", "settled first")) == ("Cancelled", "Settled first"))
    }

    @Test func unknownEventsAreShownReadably() {
        #expect(ApprovalWording.title("resolve_unmatched") == "Resolve unmatched")
    }

    @Test func pythonTimestampsParse() {
        #expect(ApprovalWording.date("2026-10-05T09:12:03.123456+00:00") != nil)
        #expect(ApprovalWording.date("2026-10-05T09:12:03+00:00") != nil)
        #expect(ApprovalWording.time("not a date") == "not a date")
    }
}

@MainActor @Suite struct CatalogTests {
    @Test func everyScreenOfThisSliceHasEntries() {
        let ids = Set(ScreenCatalog.entries().map(\.id))
        for id in ["overview-running", "overview-needs-setup", "overview-error", "overview-approvals-sheet",
                   "window-overview", "onboarding-locate", "onboarding-migration"] {
            #expect(ids.contains(id), "missing \(id)")
        }
    }

    @Test func previewStatesRouteToTheirScreens() {
        let missing = AppModel.preview(location: .failure(LocateFailure(lookedAt: ["x"])))
        #expect(missing.menuState == .notFound)
        let migrating = AppModel.preview(client: PreviewClient(status: .success(PreviewData.statusMigration)))
        #expect(migrating.menuState == .needsMigration)
    }
}
