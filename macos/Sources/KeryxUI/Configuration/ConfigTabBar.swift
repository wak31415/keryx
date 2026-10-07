import SwiftUI

/// Configuration's tabs, as a Settings window's toolbar shows them: a centred row of
/// symbol-over-name buttons, the chosen one on a rounded highlight with its symbol in the
/// accent colour. No tab is chosen while a search shows results from all of them.
struct ConfigTabBar: View {
    let tabs: [ConfigWording.Tab]
    /// The tab shown, or nil while a search is.
    let selection: String?
    let select: (String) -> Void

    var body: some View {
        HStack(spacing: 2) {
            ForEach(tabs) { tab in
                let selected = tab.id == selection
                Button { select(tab.id) } label: {
                    VStack(spacing: 3) {
                        Image(systemName: tab.symbol)
                            .font(.system(size: 17, weight: .regular))
                            .frame(height: 22)
                            .foregroundStyle(selected ? AnyShapeStyle(.tint) : AnyShapeStyle(.secondary))
                        Text(tab.title)
                            .font(.system(size: 11))
                            .foregroundStyle(selected ? .primary : .secondary)
                            .lineLimit(1)
                            .fixedSize()
                    }
                    .frame(minWidth: 54)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 5)
                    .contentShape(RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(TabButtonStyle(selected: selected))
                .accessibilityLabel(tab.title)
                .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
            }
        }
        .padding(.horizontal, 12)
        .padding(.top, 8)
        .padding(.bottom, 7)
        .frame(maxWidth: .infinity)
    }
}

/// A tab's highlight: a soft rounded fill under the chosen tab, a fainter one while a tab
/// is pressed.
private struct TabButtonStyle: ButtonStyle {
    let selected: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background {
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(.primary.opacity(selected ? 0.08 : configuration.isPressed ? 0.05 : 0))
            }
    }
}
