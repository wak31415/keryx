import KeryxKit
import SwiftUI

/// Every prompt the approval calls have handled, newest first: one row each with what last
/// happened to it, its history under a disclosure.
package struct ApprovalsSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    package init() {}

    package var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Approval Calls")
                    .font(.headline)
                if let report = model.approvals.value {
                    Text(Self.summary(report))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding([.horizontal, .top], 20)
            .padding(.bottom, 12)
            StateView(model.approvals, section: "Approval Calls",
                      retry: { Task { await model.refresh(.approvals) } }) { report in
                Table(Self.rows(report), children: \.history) {
                    TableColumn("Time") { row in
                        Text(ApprovalWording.time(row.entry.ts))
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                    }
                    .width(124)
                    TableColumn("Event") { row in
                        Text(row.title)
                    }
                    .width(190)
                    TableColumn("Prompt") { row in
                        Text(row.detail ?? "")
                            .lineLimit(1)
                            .truncationMode(.tail)
                            .help(row.detail ?? "")
                            .textSelection(.enabled)
                    }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            Divider()
            HStack {
                Spacer()
                Button("Done") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
            .padding(20)
        }
        .frame(minWidth: 620, idealWidth: 700, minHeight: 360, idealHeight: 440)
    }

    /// A line of the table: a request — what happened to it, when it was last touched —
    /// with every one of its events under it; or one of those events.
    struct Row: Identifiable {
        let id: String
        let entry: ApprovalEntry
        /// What happened: a request's outcome, or the event.
        let title: String
        /// The prompt, on the request's own line; an earlier event's only when it differs.
        let detail: String?
        /// The request's events, newest first; `nil` for an event, or a request with only
        /// the one.
        let history: [Row]?
    }

    nonisolated static func rows(_ report: ApprovalsReport) -> [Row] {
        ApprovalWording.requests(report.entries).map { request in
            let summary = request.summary
            let events = request.events.enumerated().map { index, entry in
                Row(id: "\(request.id)/\(index)", entry: entry, title: ApprovalWording.title(entry.event),
                    detail: entry.what == summary || entry.what.isEmpty ? nil : entry.what, history: nil)
            }
            return Row(id: request.id, entry: request.latest, title: request.outcome, detail: summary,
                       history: events.count > 1 ? events : nil)
        }
    }

    /// "2 answered before Keryx called · 1 at the keyboard after a call": the CLI's two
    /// counts, both of prompts settled at the keyboard, before and after Keryx rang. (The
    /// audit does not count answers given on the phone, so neither does this.)
    nonisolated static func summary(_ report: ApprovalsReport) -> String {
        "\(report.cancelledBeforeCall) answered before Keryx called · "
            + "\(report.answeredAfterCall) at the keyboard after a call"
    }
}
