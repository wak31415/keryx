import KeryxKit
import SwiftUI

/// One model in the catalog: what it is, how big, and the one thing to do with it now —
/// download it, watch it download, use it, or nothing because it is in use or too big.
struct ModelRow: View {
    /// The control at the row's trailing edge.
    enum Action: Equatable {
        case download
        case downloading(done: Int64, total: Int64)
        case failed(String)
        case use
        case inUse
        case tooBig
    }

    let entry: CatalogModel
    let action: Action
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
                // A model too big says what it needs at the trailing edge instead.
                Text(action == .tooBig ? Formatting.gigabytes(entry.sizeGb) : Self.sizes(entry))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                if case let .failed(message) = action {
                    Text(message)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .textSelection(.enabled)
                        .help(message)
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
            Button("Download", action: download)
                .buttonStyle(.bordered)
                .disabled(busy)
                .help(busy ? "One download at a time" : "Download \(Formatting.gigabytes(entry.sizeGb))")
        case let .downloading(done, total):
            HStack(spacing: 10) {
                Text(Self.progress(done: done, total: total))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                DownloadRing(fraction: total > 0 ? Double(done) / Double(total) : nil, stop: cancel)
            }
        case .failed:
            HStack(spacing: 10) {
                Text("Download failed")
                    .foregroundStyle(.orange)
                Button("Try Again", action: download)
                    .disabled(busy)
                    .help(busy ? "One download at a time" : "Download it again")
            }
        case .use:
            Button("Use This Model…", action: use)
        case .inUse:
            Label("In Use", systemImage: "checkmark")
                .foregroundStyle(.secondary)
        case .tooBig:
            Text("Needs \(Formatting.gigabytes(entry.needsGb))")
                .foregroundStyle(.secondary)
                .help("Too big for the memory this Mac has for models")
        }
    }

    /// What the row offers, from the catalog, the download (if any) and the local agent.
    static func action(for entry: CatalogModel, phase: Downloads.Phase?, inUse: Bool) -> Action {
        switch phase {
        case let .running(done, total): return .downloading(done: done, total: total)
        case let .failed(message): return .failed(message)
        case .finished, nil: break
        }
        if inUse { return .inUse }
        if entry.downloaded || phase == .finished { return .use }
        return entry.fits ? .download : .tooBig
    }

    /// "18.6 GB · needs 25 GB": the download, and the memory it takes while it runs.
    static func sizes(_ entry: CatalogModel) -> String {
        "\(Formatting.gigabytes(entry.sizeGb)) · needs \(Formatting.gigabytes(entry.needsGb))"
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
