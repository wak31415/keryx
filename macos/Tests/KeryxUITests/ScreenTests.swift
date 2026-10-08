import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
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

    @Test func aProgramsNameKeepsItsCase() {
        #expect(Wording.capitalised("llama-server is not installed") == "llama-server is not installed")
        #expect(Wording.capitalised("install-launchd.sh failed") == "install-launchd.sh failed")
        #expect(Wording.capitalised("cloudflared is not on PATH") == "cloudflared is not on PATH")
        #expect(Wording.capitalised("uv: not found") == "uv: not found")
        #expect(Wording.capitalised("could not reach 127.0.0.1") == "Could not reach 127.0.0.1")
        #expect(Wording.capitalised("track down why") == "Track down why")
    }

    @Test func aKeyBecomesItsTitleInQuotationMarks() {
        let titles = ["ALLOWED_CALLERS": "Your phone numbers", "VOICE_BASE_URL": "Voice server URL"]
        #expect(Wording.named("ALLOWED_CALLERS is empty — every call is refused", titles: titles)
            == "“Your phone numbers” is empty — every call is refused")
        #expect(Wording.named("unless VOICE_BASE_URL names one; CODEX_HOME is its own", titles: titles)
            == "unless “Voice server URL” names one; CODEX_HOME is its own", "a key with no row stays")
        #expect(CheckWording.detail("ALLOWED_CALLERS is empty", titles: titles).text
            == "“Your phone numbers” is empty")
    }

    @Test func aServicesLabelIsSaidAsWhatItIs() {
        let raw = "dev.keryx.agent is not installed — Keryx cannot restart itself, and nothing restarts it if it dies"
        let said = CheckWording.detail(raw)
        #expect(said.text == "Keryx isn't answering calls yet — Keryx cannot restart itself, and nothing "
            + "restarts it if it dies")
        #expect(said.raw == raw, "the label under Show Details")
        #expect(CheckWording.detail("keryx.service is not installed").text == "Keryx isn't answering calls yet")
        #expect(CheckWording.detail("llama-server is not installed").raw == nil, "a program is not a service")
    }

    @Test func anOptionalCheckLeftOffIsGrey() {
        #expect(CheckGlyph.of("missing", detail: "off (optional)").symbol == CheckGlyph.off.symbol)
        #expect(CheckGlyph.of("missing", detail: "not set").symbol == CheckGlyph.missing.symbol)
        #expect(CheckGlyph.of("failed", detail: "off (optional)").symbol == CheckGlyph.failed.symbol)
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
        #expect(ApprovalsSheet.summary(report) == "2 answered before Keryx called · 1 at the keyboard after a call")
    }

    @Test func eventsGatherByRequestNewestFirst() {
        let entries = [
            ApprovalEntry(ts: "1", event: "started", what: "", requestId: nil),
            ApprovalEntry(ts: "2", event: "raised", what: "git push", requestId: .number(17)),
            ApprovalEntry(ts: "3", event: "raised", what: "rm -rf build/", requestId: .number(18)),
            ApprovalEntry(ts: "4", event: "escalated", what: "git push", requestId: .number(17)),
            ApprovalEntry(ts: "5", event: "armed", what: "git push", requestId: .number(17)),
            ApprovalEntry(ts: "6", event: "settled", what: "git push", requestId: .number(17)),
        ]
        let requests = ApprovalWording.requests(entries)
        #expect(requests.map(\.id) == ["request-17", "request-18", "event-0"])
        #expect(requests[0].events.map(\.ts) == ["6", "5", "4", "2"])
        #expect(requests[0].summary == "git push")
        #expect(requests[0].outcome == "Read out the choices", "settled says nothing about how")
        #expect(requests[1].outcome == "Claude Code asked")
        #expect(requests[2].standalone)
        let rows = ApprovalsSheet.rows(ApprovalsReport(escalationEnabled: true, escalateAfterS: 300, maxPerHour: 4,
                                                       cancelledBeforeCall: 0, answeredAfterCall: 0,
                                                       entries: entries))
        #expect(rows[0].history?.count == 4)
        #expect(rows[1].history == nil)
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
                   "overview-approvals-off",
                   "window-overview", "onboarding-locate", "onboarding-locate-details", "onboarding-migration"] {
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
