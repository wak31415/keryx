import KeryxKit
import SwiftUI

/// What the models on this Mac take of the memory it gives them: the agent's model, the
/// voice's own LLM, and the speech models — each the catalog's own figure for it, as `keryx
/// models list` prints it. Information only: whether they fit is the CLI's to say (`fits`,
/// `fits_with_voice_llm`, and doctor), never added up and judged here.
struct MemoryShares: Equatable {
    struct Share: Equatable, Identifiable {
        enum Kind: Equatable { case agent, voiceLLM, speech }
        var kind: Kind
        var title: String
        var gigabytes: Double
        /// A figure the CLI does not print, so the app's own reckoning: said as "about".
        var approximate = false
        var id: String { title }
    }

    var shares: [Share]

    /// The speech models' share while the voice runs here, approximately: the CLI reserves
    /// `catalog.VOICE_SERVER_GB` for them without printing it.
    static let speechGigabytes = 3.0

    /// The shares as things are set up: the agent's model served here, the voice's own model
    /// while the voice takes its words from it, and the speech models while the voice is here.
    static func of(_ report: ModelsReport, voice: VoiceStatus?) -> MemoryShares {
        var shares: [Share] = []
        if let name = report.servers.llm.model, let entry = report.models.first(where: { $0.name == name }) {
            shares.append(Share(kind: .agent, title: "Agent: \(entry.title)", gigabytes: entry.needsGb))
        }
        if let llm = voice?.llm, llm.source == VoiceLLMSource.here.rawValue, let name = llm.model,
           let entry = report.models.first(where: { $0.name == name }) {
            shares.append(Share(kind: .voiceLLM, title: "Voice LLM: \(entry.title)", gigabytes: entry.needsGb))
        }
        if voice?.kind == .here {
            shares.append(Share(kind: .speech, title: "Speech models", gigabytes: speechGigabytes, approximate: true))
        }
        return MemoryShares(shares: shares)
    }
}

/// `MemoryShares` as one bar under This Mac's memory — each share a segment, as a part of
/// what the Mac gives models — and a legend. It says nothing of whether they fit.
struct MemoryBar: View {
    let shares: MemoryShares
    let available: Double

    var body: some View {
        if !shares.shares.isEmpty, available > 0 {
            VStack(alignment: .leading, spacing: 6) {
                GeometryReader { geometry in
                    HStack(spacing: 1) {
                        ForEach(Self.widths(shares, available: available, total: geometry.size.width), id: \.id) { part in
                            Rectangle()
                                .fill(Self.colour(part.kind))
                                .frame(width: part.width)
                        }
                        Spacer(minLength: 0)
                    }
                    .background(.quaternary)
                    .clipShape(RoundedRectangle(cornerRadius: 4))
                }
                .frame(height: 8)
                .accessibilityHidden(true)
                HStack(spacing: 14) {
                    ForEach(shares.shares) { share in
                        HStack(spacing: 5) {
                            Circle().fill(Self.colour(share.kind)).frame(width: 7, height: 7)
                            Text(Self.legend(share))
                        }
                    }
                    Spacer(minLength: 0)
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            .padding(.vertical, 2)
            .accessibilityElement(children: .combine)
        }
    }

    /// "Agent: Qwen3-Coder 30B-A3B · 25 GB", "Speech models · about 3 GB".
    static func legend(_ share: MemoryShares.Share) -> String {
        "\(share.title) · \(share.approximate ? "about " : "")\(Formatting.gigabytes(share.gigabytes))"
    }

    /// Each share's segment, as a part of `available` drawn `total` wide — of the shares
    /// together, when they come to more, so each is still seen in proportion. Drawing only.
    static func widths(_ shares: MemoryShares, available: Double, total: Double)
        -> [(id: String, kind: MemoryShares.Share.Kind, width: Double)] {
        let scale = max(available, shares.shares.reduce(0) { $0 + $1.gigabytes })
        return shares.shares.map { ($0.id, $0.kind, total * $0.gigabytes / scale) }
    }

    static func colour(_ kind: MemoryShares.Share.Kind) -> Color {
        switch kind {
        case .agent: .accentColor
        case .voiceLLM: .teal
        case .speech: .purple
        }
    }
}
