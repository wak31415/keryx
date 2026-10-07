import KeryxKit
import SwiftUI

/// Every approval-call event the audit log keeps, newest first.
struct ApprovalsSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    var body: some View {
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
            StateView(model.approvals, retry: { Task { await model.refresh(.approvals) } }) { report in
                Table(rows(report)) {
                    TableColumn("Time") { row in
                        Text(ApprovalWording.time(row.entry.ts))
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                    }
                    .width(124)
                    TableColumn("Event") { row in
                        Text(row.title)
                    }
                    .width(170)
                    TableColumn("Detail") { row in
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

    private struct Row: Identifiable {
        let id: Int
        let entry: ApprovalEntry
        let title: String
        let detail: String?
    }

    private func rows(_ report: ApprovalsReport) -> [Row] {
        report.entries.enumerated().reversed().map { index, entry in
            let (title, detail) = ApprovalWording.describe(entry)
            return Row(id: index, entry: entry, title: title, detail: detail)
        }
    }

    /// "2 answered before Keryx called · 1 answered at the keyboard after": the CLI's two
    /// counts, both of prompts settled at the keyboard, before and after Keryx rang.
    static func summary(_ report: ApprovalsReport) -> String {
        "\(report.cancelledBeforeCall) answered before Keryx called · "
            + "\(report.answeredAfterCall) answered at the keyboard after"
    }
}
