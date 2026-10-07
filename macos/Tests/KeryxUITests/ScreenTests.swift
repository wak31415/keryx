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

    @Test func everyBrokerEventHasItsOwnWords() {
        // The events keryx/approvals/broker.py writes.
        let events = ["started", "raised", "escalated", "escalation_skipped", "escalation_failed", "armed",
                      "keypad_unrecognised", "verdict_discarded", "settled", "resolve_fallback",
                      "resolve_unmatched"]
        let titles = events.map(ApprovalWording.title)
        #expect(Set(titles).count == events.count)
        for (event, title) in zip(events, titles) {
            #expect(!title.contains("_"), "\(event) reads as \(title)")
        }
        #expect(ApprovalWording.title("escalated") == "Called you")
    }

    @Test func unknownEventsAreTheirNameHumanised() {
        #expect(ApprovalWording.title("answered_at_keyboard") == "Answered at keyboard")
        #expect(ApprovalWording.title("something_new") == "Something new")
    }

    @Test func whatIsShownAsTheSessionWroteIt() {
        #expect(ApprovalWording.describe(entry("escalated", "rm -rf build/")) == ("Called you", "rm -rf build/"))
        #expect(ApprovalWording.describe(entry("settled", "")) == ("Settled", nil))
    }

    @Test func theSheetsCountsSayWhatTheCLICounts() {
        var report = PreviewData.approvals
        report.cancelledBeforeCall = 2
        report.answeredAfterCall = 1
        #expect(ApprovalsSheet.summary(report) == "2 answered before Keryx called · 1 answered at the keyboard after")
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
