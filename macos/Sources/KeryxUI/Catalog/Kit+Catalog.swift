import KeryxKit
import SwiftUI

extension ScreenCatalog {
    /// The mark, the menu-bar images and the small pieces every screen uses.
    @MainActor static var kitEntries: [Entry] {
        [
            Entry("mark", size: CGSize(width: 360, height: 220)) { MarkSheet() },
            Entry("menubar-icons", size: CGSize(width: 360, height: 180)) { MenuBarIconSheet() },
            Entry("kit", size: CGSize(width: 900, height: 460)) { KitSheet() },
        ]
    }
}

// MARK: - The design kit's own sheets

/// The mark at 128 pt, idle, beside its smaller sizes and its template (single-colour) form.
private struct MarkSheet: View {
    var body: some View {
        HStack(spacing: 28) {
            KeryxMark().frame(width: 128, height: 128)
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .center, spacing: 14) {
                    KeryxMark().frame(width: 64, height: 64)
                    KeryxMark().frame(width: 32, height: 32)
                    KeryxMark().frame(width: 16, height: 16)
                }
                HStack(alignment: .center, spacing: 14) {
                    KeryxMark(colored: false).frame(width: 64, height: 64)
                    KeryxMark(colored: false).frame(width: 32, height: 32)
                    KeryxMark(colored: false).frame(width: 16, height: 16)
                }
                Text("Keryx").font(.title2.weight(.semibold))
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// The three menu-bar images on a strip at their real 18 pt, then four times larger.
private struct MenuBarIconSheet: View {
    var body: some View {
        VStack(spacing: 16) {
            HStack(spacing: 14) {
                ForEach(MenuSymbol.allCases, id: \.self) { symbol in
                    icon(symbol).frame(width: 18, height: 18)
                }
            }
            .padding(.horizontal, 12)
            .frame(height: 24)
            .background(.quaternary, in: Capsule())
            HStack(spacing: 28) {
                ForEach(MenuSymbol.allCases, id: \.self) { symbol in
                    VStack(spacing: 6) {
                        icon(symbol).frame(width: 72, height: 72)
                        Text(String(describing: symbol)).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func icon(_ symbol: MenuSymbol) -> some View {
        Image(nsImage: MenuBarIcon.image(symbol))
            .renderingMode(.template)
            .resizable()
            .foregroundStyle(.primary)
    }
}

/// The small pieces every screen uses: status dots, and a `StateView` in each state.
private struct KitSheet: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 18) {
                LabeledDot(tone: .ok, text: "Running")
                LabeledDot(tone: .warning, text: "Needs attention")
                LabeledDot(tone: .error, text: "Failed")
                LabeledDot(tone: .inactive, text: "Stopped")
            }
            Text("\(Formatting.gigabytes(18.6)) · \(Formatting.cost(3.41)) · \(Formatting.relative(Date(timeIntervalSinceNow: -7200)))")
                .foregroundStyle(.secondary)
            HStack(spacing: 12) {
                panel {
                    StateView(value: nil as String?, isLoading: true, failure: nil) { Text($0) }
                }
                panel {
                    StateView(value: nil as String?, isLoading: false,
                              failure: .init(title: KeryxError.timedOut(command: "keryx status").title,
                                             message: KeryxError.timedOut(command: "keryx status").message),
                              retry: {}) { Text($0) }
                }
                panel {
                    StateView(value: "Loaded content", isLoading: false, failure: nil) { Text($0) }
                }
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func panel(@ViewBuilder _ content: () -> some View) -> some View {
        content()
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(.quinary, in: RoundedRectangle(cornerRadius: 8))
    }
}

private struct LabeledDot: View {
    let tone: StatusDot.Tone
    let text: String

    var body: some View {
        HStack(spacing: 6) {
            StatusDot(tone)
            Text(text)
        }
    }
}
