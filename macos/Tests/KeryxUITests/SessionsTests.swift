import Foundation
import Testing
@testable import KeryxKit
@testable import KeryxUI

@MainActor @Suite struct SessionsTests {
    let us = Locale(identifier: "en_US")
    var tasks: [TaskItem] { PreviewData.tasks.tasks }

    // MARK: - The list

    @Test func housekeepingIsHiddenUntilAskedFor() {
        let ids = SessionWording.visible(tasks, filter: nil, housekeeping: false).map(\.id)
        #expect(!ids.contains(37))
        #expect(SessionWording.visible(tasks, filter: nil, housekeeping: true).map(\.id).contains(37))
    }

    @Test func runningLetsThroughTheQueuedToo() {
        let running = SessionWording.visible(tasks, filter: .running, housekeeping: false).map(\.id)
        #expect(running == [42, 41])
        #expect(SessionWording.visible(tasks, filter: .done, housekeeping: false).map(\.id) == [40, 39])
        #expect(SessionWording.visible(tasks, filter: .failed, housekeeping: false).map(\.id) == [38])
        #expect(SessionWording.filters.map(SessionWording.filterTitle) == ["All", "Running", "Done", "Failed"])
    }

    @Test func footerCountsSessionsAndCostWithoutHousekeeping() {
        let usage = PreviewData.usage
        #expect(SessionWording.footer(usage, housekeeping: false, locale: us)
            == "Last 30 days: 22 sessions · $47.77 estimated")
        #expect(SessionWording.footer(usage, housekeeping: true, locale: us)
            == "Last 30 days: 31 sessions · $47.77 estimated")
    }

    @Test func footerLeavesOutACostNobodyEstimated() {
        let usage = UsageReport(since: nil, projects: [
            ProjectUsage(label: "keryx", tasks: 1, priced: 0, inputTokens: 0, outputTokens: 0, costUsd: nil),
        ])
        #expect(SessionWording.footer(usage, housekeeping: false, locale: us) == "Last 30 days: 1 session")
    }

    @Test func rowBylineNamesTheAgentProjectAndWhen() {
        let task = tasks[1]
        let byline = SessionWording.rowByline(task, now: task.createdAt.addingTimeInterval(7200), locale: us)
        #expect(byline == "Claude · keryx · 2 hours ago")
    }

    // MARK: - The timeline

    @Test func headerBylineSaysWhereItWasAsked() {
        let task = tasks[2]
        let byline = SessionWording.headerByline(task, now: task.startedAt!.addingTimeInterval(7200), locale: us)
        #expect(byline == "Claude (claude-opus-5-5) · keryx · started 2 hours ago · via phone")
        #expect(SessionWording.channel("local") == "on this Mac")
        #expect(SessionWording.channel(nil) == nil)
    }

    @Test func statusTitleSaysHowLongItTook() {
        #expect(SessionWording.statusTitle(tasks[2], locale: us) == "Done in 15 minutes")
        #expect(SessionWording.statusTitle(tasks[4], locale: us) == "Failed after 7 minutes")
        #expect(SessionWording.statusTitle(tasks[1], locale: us) == "Running")
        #expect(SessionWording.statusTitle(tasks[0], locale: us) == "Waiting to start")
        #expect(SessionWording.statusTitle(tasks[5], locale: us) == "Cancelled after under a minute")
    }

    @Test func usageIsTokensAndAnEstimate() {
        #expect(SessionWording.usage(tasks[2], locale: us) == "48K in · 6.1K out · $1.84 estimated")
        #expect(SessionWording.usage(tasks[4], locale: us) == "12K in · 980 out · $0.31 estimated")
        #expect(SessionWording.usage(tasks[1], locale: us) == nil)
    }

    @Test func followUpsSplitTheStepsIntoStretches() {
        let stretches = SessionWording.stretches(PreviewData.taskDetail.log)
        #expect(stretches.count == 3)
        guard case let .steps(first) = stretches[0], case let .followup(followup) = stretches[1],
              case let .steps(second) = stretches[2] else {
            Issue.record("unexpected stretches: \(stretches)")
            return
        }
        #expect(first.count == 8)
        #expect(followup.text.hasPrefix("while you're in there"))
        #expect(second.count == 7)
        #expect(Set(stretches.map(\.id)).count == 3)
    }

    @Test func reportHeadingsParagraphsListsAndCode() {
        let blocks = ReportText.blocks("## What I did\n\nAdded `x`.\nSecond line.\n\n- one\n* two\n\n```\nlet a = 1\n```\nAfter")
        #expect(blocks == [
            .heading("What I did"),
            .paragraph("Added `x`.\nSecond line."),
            .paragraph("•  one\n•  two"),
            .code("let a = 1"),
            .paragraph("After"),
        ])
    }

    /// The header `TaskManager._write_report` puts above every report ends in a rule, which
    /// is a break between blocks, not a paragraph reading "---".
    @Test func aRuleSeparatesBlocksAndIsNotText() {
        #expect(ReportText.blocks("# Task 40\n\nadd the flag\n\n---\n\n## What I did\n\nDone.\n* * *\nAfter") == [
            .heading("Task 40"),
            .paragraph("add the flag"),
            .heading("What I did"),
            .paragraph("Done."),
            .paragraph("After"),
        ])
    }

    // MARK: - The catalog

    @Test func everySessionsStateIsInTheCatalog() {
        let ids = Set(ScreenCatalog.entries().map(\.id))
        for id in ["sessions-running", "sessions-done-followup", "sessions-failed", "sessions-untold",
                   "sessions-no-selection", "sessions-empty", "sessions-filtered-empty", "sessions-error", "window-sessions"] {
            #expect(ids.contains(id), "missing \(id)")
        }
    }
}
