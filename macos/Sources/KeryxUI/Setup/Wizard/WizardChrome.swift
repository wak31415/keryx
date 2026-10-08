import KeryxKit
import SwiftUI

/// The wizard's window: one fixed size, every frame laid out the same way.
package enum WizardMetrics {
    package static let size = CGSize(width: 640, height: 640)
    /// The width the frame's sections and sentences keep to.
    static let column: CGFloat = 540
}

/// The large symbol at the top of a frame.
enum WizardSymbol {
    /// The Keryx mark.
    case mark
    case system(String, Color? = nil)
}

/// One frame: a symbol, a title, the dot strip, one sentence, the frame's sections, and its
/// buttons — the leading ones (Back, a checkbox) on the left, the rest on the right with the
/// primary last.
struct WizardPage<Content: View, Leading: View, Trailing: View>: View {
    let symbol: WizardSymbol
    let title: String
    let sentence: String
    /// "Step 3 of 6": the frame shown, of all this run shows; `nil` hides the strip.
    var position: (index: Int, count: Int)?
    /// Welcome's and You're Ready's: a larger symbol, lower in the window.
    var hero = false
    @ViewBuilder var content: Content
    @ViewBuilder var leading: Leading
    @ViewBuilder var trailing: Trailing

    var body: some View {
        VStack(spacing: 0) {
            VStack(spacing: 8) {
                icon
                    .frame(height: hero ? 88 : 38)
                    .padding(.bottom, hero ? 14 : 2)
                    .accessibilityHidden(true)
                Text(title)
                    .font(hero ? .largeTitle.weight(.semibold) : .title2.weight(.semibold))
                    .accessibilityAddTraits(.isHeader)
                if let position, position.count > 1 {
                    StepDots(index: position.index, count: position.count)
                }
                Text(sentence)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: 460)
                    .padding(.top, 2)
            }
            .padding(.top, hero ? 120 : 24)
            .padding(.horizontal, 40)

            // In a stack of its own, so an empty frame still takes the room.
            VStack(spacing: 0) { content }
                .frame(maxWidth: WizardMetrics.column)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                .padding(.top, 16)
                .padding(.horizontal, 24)

            HStack(spacing: 10) {
                leading
                Spacer(minLength: 12)
                trailing
            }
            .controlSize(.large)
            .padding(.horizontal, 24)
            .padding(.bottom, 20)
            .padding(.top, 12)
        }
        .frame(width: WizardMetrics.size.width, height: WizardMetrics.size.height)
    }

    @ViewBuilder private var icon: some View {
        switch symbol {
        case .mark:
            KeryxMark().frame(width: hero ? 88 : 38, height: hero ? 88 : 38)
        case let .system(name, colour):
            Image(systemName: name)
                .font(.system(size: hero ? 72 : 32, weight: .light))
                .foregroundStyle(colour.map { AnyShapeStyle($0) } ?? AnyShapeStyle(.tint))
                .symbolRenderingMode(.hierarchical)
        }
    }
}

extension WizardPage where Leading == EmptyView {
    init(symbol: WizardSymbol, title: String, sentence: String, position: (index: Int, count: Int)?,
         hero: Bool = false, @ViewBuilder content: () -> Content, @ViewBuilder trailing: () -> Trailing) {
        self.init(symbol: symbol, title: title, sentence: sentence, position: position, hero: hero,
                  content: content, leading: { EmptyView() }, trailing: trailing)
    }
}

extension SetupWizard {
    /// Reads the plan and the settings again (what Continue saved), then moves on to the next
    /// frame that has something to ask.
    func moveOn(_ model: AppModel) async {
        await model.refresh(.setupPlan, .config)
        advance(WizardFacts(model))
    }
}

/// A path as a person reads it: the home folder as `~`.
enum WizardPaths {
    static func short(_ path: String) -> String {
        guard let range = path.range(of: #"^/Users/[^/]+"#, options: .regularExpression) else { return path }
        return "~" + path[range.upperBound...]
    }
}

/// Where the run is: a dot per frame, the current one in the accent colour and wider.
struct StepDots: View {
    let index: Int
    let count: Int

    var body: some View {
        HStack(spacing: 6) {
            ForEach(1...max(count, 1), id: \.self) { dot in
                Capsule()
                    .fill(dot == index ? AnyShapeStyle(.tint) : dot < index
                        ? AnyShapeStyle(.secondary) : AnyShapeStyle(.quaternary))
                    .frame(width: dot == index ? 18 : 6, height: 6)
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(WizardWording.stepOf(index, count))
        .help(WizardWording.stepOf(index, count))
    }
}

/// A part of a frame that asks one thing: a header, and what it asks. Done, it can fold to
/// one line with a tick — the frame gets calmer as it is answered, never longer.
struct WizardSection<Content: View>: View {
    let title: String
    /// What the header says it is doing: done, still working, or nothing.
    var state: SectionState = .open
    @ViewBuilder var content: Content

    enum SectionState: Equatable {
        case open
        /// Answered; its work (a download, a sign-in) is under way.
        case working
        case done
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text(title)
                    .font(.headline)
                Spacer(minLength: 0)
                switch state {
                case .open: EmptyView()
                case .working:
                    ProgressView().controlSize(.small)
                        .accessibilityLabel("Working")
                case .done:
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                        .accessibilityLabel("Done")
                }
            }
            content
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quinary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

/// A section that is set: one line, a tick, what it is set to, and Change.
struct FoldedSection: View {
    let title: String
    let summary: String
    var change: (() -> Void)?

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
                .accessibilityLabel("Done")
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.headline)
                Text(summary)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: 8)
            if let change {
                Button("Change") { change() }
                    .controlSize(.regular)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quinary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

/// A choice that must be seen to be made: cards side by side, one chosen. A card that
/// cannot be chosen is greyed with why under it, and is never hidden.
struct RadioCards<Choice: Hashable & Identifiable>: View {
    let choices: [Choice]
    @Binding var selection: Choice
    let title: (Choice) -> String
    let caption: (Choice) -> String
    var unavailable: (Choice) -> String? = { _ in nil }
    var disabled = false

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            ForEach(choices) { choice in
                card(choice)
            }
        }
        .accessibilityElement(children: .contain)
    }

    private func card(_ choice: Choice) -> some View {
        let chosen = choice == selection
        let why = unavailable(choice)
        return Button {
            selection = choice
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Image(systemName: chosen ? "largecircle.fill.circle" : "circle")
                        .foregroundStyle(chosen ? AnyShapeStyle(.tint) : AnyShapeStyle(.secondary))
                        .imageScale(.medium)
                    Text(title(choice))
                        .font(.callout.weight(.semibold))
                        .lineLimit(1)
                }
                Text(why ?? caption(choice))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(10)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(chosen ? AnyShapeStyle(.tint.opacity(0.12)) : AnyShapeStyle(.background.opacity(0.6))))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(chosen ? AnyShapeStyle(.tint) : AnyShapeStyle(.separator), lineWidth: chosen ? 1.5 : 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(disabled || why != nil)
        .opacity(why != nil ? 0.55 : 1)
        .accessibilityLabel(title(choice))
        .accessibilityHint(why ?? caption(choice))
        .accessibilityAddTraits(chosen ? [.isSelected] : [])
    }
}

/// A line at a section's foot: what its work is doing, or how it ended.
struct StatusLine: View {
    enum Tone: Equatable { case none, working, ok, problem, warning }

    let tone: Tone
    let text: String
    /// Determinate progress, 0…1.
    var fraction: Double?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                switch tone {
                case .none:
                    Image(systemName: "circle.dashed").foregroundStyle(.secondary)
                case .working:
                    ProgressView().controlSize(.small)
                case .ok:
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                case .problem:
                    Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
                case .warning:
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                }
                Text(text)
                    .foregroundStyle(tone == .none ? .secondary : .primary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let fraction {
                ProgressView(value: fraction)
                    .progressViewStyle(.linear)
            }
        }
        .font(.callout)
    }
}
