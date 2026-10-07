import KeryxKit
import SwiftUI

/// One session in the list, the way Mail shows a message: an unread dot while the result is
/// still to be told, what was asked, and who did it, where and when beneath.
struct SessionRow: View {
    let task: TaskItem

    @Environment(\.backgroundProminence) private var prominence

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Circle()
                .fill(unread ? dotStyle : AnyShapeStyle(.clear))
                .frame(width: 8, height: 8)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 3) {
                Text(Wording.capitalised(task.description))
                    .font(.headline)
                    .lineLimit(2)
                    .truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Text(SessionWording.rowByline(task))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .help(task.createdAt.formatted(date: .complete, time: .shortened))
            }

            status
                .frame(width: 16)
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
        .accessibilityValue(accessibilityStatus)
    }

    private var unread: Bool { task.told == false }

    /// The tint, but white on a selected row's accent fill, as Mail draws its unread dot.
    private var dotStyle: AnyShapeStyle {
        prominence == .increased ? AnyShapeStyle(.white) : AnyShapeStyle(.tint)
    }

    @ViewBuilder private var status: some View {
        switch task.status {
        case .running:
            ProgressView()
                .controlSize(.small)
        case .failed:
            Image(systemName: "xmark.circle")
                .foregroundStyle(prominence == .increased ? AnyShapeStyle(.white) : AnyShapeStyle(.red))
        case .queued:
            Image(systemName: "clock").foregroundStyle(.secondary)
        case .cancelled:
            Image(systemName: "minus.circle").foregroundStyle(.secondary)
        case .done:
            EmptyView()
        }
    }

    private var accessibilityStatus: String {
        var words = [SessionWording.statusTitle(task)]
        if unread { words.append("not yet told") }
        return words.joined(separator: ", ")
    }
}
