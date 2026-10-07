import KeryxKit
import SwiftUI

extension ScreenCatalog {
    /// The menu bar menu. The real one is an `NSMenu`; this draws its items in a column
    /// shaped like one, for review.
    @MainActor static var menuEntries: [Entry] {
        [
            Entry("menu", size: CGSize(width: 360, height: 380)) {
                MenuSheet().environment(AppModel.preview())
            },
        ]
    }
}

/// `MenuContent` laid out like a menu: one line per item, a checkmark column, and dividers
/// inset from the panel's edges.
private struct MenuSheet: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            MenuContent(actions: MenuActions(openWindow: {}, openSession: { _ in }, quit: {}))
                .buttonStyle(MenuItemStyle())
                .toggleStyle(MenuToggleStyle())
                .lineLimit(1)
                // A menu draws its text-only items disabled.
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, 5)
        .padding(.leading, 26)
        .padding(.trailing, 12)
        .frame(width: 320)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10))
        .padding(20)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
    }
}

private struct MenuItemStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(.primary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 3)
    }
}

/// A checkmark in the column left of the item, as a menu draws a toggle's state.
private struct MenuToggleStyle: ToggleStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(.primary)
            .padding(.vertical, 3)
            .overlay(alignment: .leading) {
                Image(systemName: "checkmark")
                    .font(.caption.weight(.semibold))
                    .opacity(configuration.isOn ? 1 : 0)
                    .offset(x: -17)
            }
    }
}
