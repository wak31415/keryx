import KeryxKit
import SwiftUI

/// One model in the catalog: what it is, how big, and the one thing to do with it now —
/// download it, watch it download, use it, or nothing because it is in use or too big.
struct ModelRow: View {
    /// The control at the row's trailing edge.
    enum Action: Equatable {
        case download
        case downloading(done: Int64, total: Int64)
        /// Stopping a download: the ring spins and cannot be pressed again.
        case cancelling
        case failed(StreamFailure)
        case use
        case inUse
        case tooBig
    }

    let entry: CatalogModel
    let action: Action
    /// The memory this Mac has for models, for why one too big does not fit.
    var available: Double?
    /// Another download is under way: only one runs at a time.
    var busy = false
    var download: () -> Void = {}
    var cancel: () -> Void = {}
    var use: () -> Void = {}

    var body: some View {
        HStack(spacing: 16) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(entry.title)
                        .font(.headline)
                    if entry.recommended { Tag(text: "Recommended") }
                    if entry.tested { Tag(text: "Tested") }
                }
                Text(Wording.capitalised(entry.note))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                Text(Self.sizes(entry))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                if action == .tooBig {
                    Text(Self.tooBig(entry, available: available))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                if case let .failed(failure) = action {
                    Text(Wording.sentence(failure.message))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .textSelection(.enabled)
                        .help(failure.message)
                    if let detail = failure.detail {
                        DetailsDisclosure(detail)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            trailing
                .fixedSize()
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder private var trailing: some View {
        switch action {
        case .download:
            // The tooltip is on what holds the button, so it still shows while the button
            // is greyed out.
            HStack {
                Button("Download", action: download)
                    .buttonStyle(.bordered)
                    .disabled(busy)
            }
            .help(busy ? Self.oneAtATime : "Download \(Formatting.gigabytes(entry.sizeGb))")
        case let .downloading(done, total):
            HStack(spacing: 10) {
                Text(Self.progress(done: done, total: total))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                DownloadRing(fraction: total > 0 ? Double(done) / Double(total) : nil, stop: cancel)
            }
        case .cancelling:
            HStack(spacing: 10) {
                Text("Stopping…")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                DownloadRing(fraction: nil, stop: {})
                    .opacity(0.4)
                    .disabled(true)
            }
        case .failed:
            HStack(spacing: 10) {
                Label {
                    Text("Download failed")
                } icon: {
                    Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
                }
                HStack {
                    Button("Try Again", action: download)
                        .disabled(busy)
                }
                .help(busy ? Self.oneAtATime : "Download it again")
            }
        case .use:
            Button("Use This Model…", action: use)
        case .inUse:
            Label("In Use", systemImage: "checkmark")
                .foregroundStyle(.secondary)
        case .tooBig:
            // Its reason is said under its name.
            EmptyView()
        }
    }

    static let oneAtATime = "One download at a time"

    /// Why a model is not offered, under the memory it needs: "Too big: this Mac has 27 GB
    /// for models."
    static func tooBig(_ entry: CatalogModel, available: Double?) -> String {
        guard let available else { return "Too big for the memory this Mac has for models." }
        return "Too big: this Mac has \(Formatting.gigabytes(available)) for models."
    }

    /// What the row offers, from the catalog, the download (if any) and the local agent.
    static func action(for entry: CatalogModel, phase: Downloads.Phase?, inUse: Bool) -> Action {
        switch phase {
        case let .running(done, total): return .downloading(done: done, total: total)
        case .cancelling: return .cancelling
        case let .failed(message): return .failed(message)
        case .finished, nil: break
        }
        if inUse { return .inUse }
        if entry.downloaded || phase == .finished { return .use }
        return entry.fits ? .download : .tooBig
    }

    /// "18.6 GB download · 25 GB memory": what it downloads, and the memory it takes while
    /// it runs.
    static func sizes(_ entry: CatalogModel) -> String {
        "\(Formatting.gigabytes(entry.sizeGb)) download · \(Formatting.gigabytes(entry.needsGb)) memory"
    }

    /// "4.2 of 7.1 GB", in the total's unit; "Starting…" until the size is known.
    static func progress(done: Int64, total: Int64, locale: Locale = .autoupdatingCurrent) -> String {
        guard total > 0 else { return "Starting…" }
        // Tenths of a gigabyte; whole megabytes.
        let units: [(Double, String, Int)] = [(1e12, "TB", 1), (1e9, "GB", 1), (1e6, "MB", 0), (1e3, "KB", 0)]
        let (scale, unit, digits) = units.first { Double(total) >= $0.0 } ?? (1, "bytes", 0)
        let style = FloatingPointFormatStyle<Double>.number.precision(.fractionLength(digits)).locale(locale)
        let shown = Double(min(done, total)) / scale
        return "\(shown.formatted(style)) of \((Double(total) / scale).formatted(style)) \(unit)"
    }
}

/// A small capsule label beside a model's name: "Recommended", "Tested".
private struct Tag: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.caption.weight(.medium))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 6)
            .padding(.vertical, 1)
            .background(.quaternary, in: .capsule)
    }
}

/// A download's progress as a ring with a stop button in it, as the App Store draws one.
/// (AppKit's determinate circular indicator is a filled pie, and reads as a status dot at
/// this size.)
private struct DownloadRing: View {
    /// `nil` until the size is known: the ring spins.
    let fraction: Double?
    let stop: () -> Void

    var body: some View {
        Button(action: stop) {
            ZStack {
                if let fraction {
                    Circle()
                        .stroke(.quaternary, lineWidth: 2)
                    Circle()
                        .trim(from: 0, to: max(0.02, min(fraction, 1)))
                        .stroke(.tint, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                } else {
                    ProgressView()
                        .progressViewStyle(.circular)
                        .controlSize(.small)
                }
                Image(systemName: "stop.fill")
                    .font(.system(size: 6, weight: .bold))
                    .foregroundStyle(.tint)
            }
            .frame(width: 20, height: 20)
            .contentShape(.circle)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Stop Downloading")
        .accessibilityValue(fraction.map { $0.formatted(.percent.precision(.fractionLength(0))) } ?? "Starting")
        .help("Stop Downloading")
    }
}
