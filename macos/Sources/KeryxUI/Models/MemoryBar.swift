import KeryxKit
import SwiftUI

/// What the models on this Mac take of the memory it gives them: the agent's model, the
/// voice's own LLM, and the speech models — each the catalog's own figure for it, as `keryx
/// models list` prints it. It adds them up to draw them; whether a model fits is the CLI's
/// to say (`fits`, `fits_with_voice_llm`), never worked out here.
struct MemoryShares: Equatable {
    struct Share: Equatable, Identifiable {
        enum Kind: Equatable { case agent, voiceLLM, speech }
        var kind: Kind
        var title: String
        var gigabytes: Double
        var id: String { title }
    }

    var shares: [Share]

    var total: Double { shares.reduce(0) { $0 + $1.gigabytes } }

    /// The speech models' share while the voice runs here. The CLI reserves it
    /// (`catalog.VOICE_SERVER_GB`) without printing it.
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
            shares.append(Share(kind: .speech, title: "Speech models", gigabytes: speechGigabytes))
        }
        return MemoryShares(shares: shares)
    }
}

/// `MemoryShares` as one bar under This Mac's memory: a segment for each, the rest free,
/// and a legend. Over what the Mac gives models, the bar says so in the warning colour.
struct MemoryBar: View {
    let shares: MemoryShares
    let available: Double

    var body: some View {
        if !shares.shares.isEmpty, available > 0 {
            let over = shares.total > available
            VStack(alignment: .leading, spacing: 6) {
                GeometryReader { geometry in
                    HStack(spacing: 1) {
                        ForEach(shares.shares) { share in
                            Rectangle()
                                .fill(Self.colour(share.kind))
                                .frame(width: geometry.size.width * min(share.gigabytes, available)
                                    / max(shares.total, available))
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
                            Text("\(share.title) · \(Formatting.gigabytes(share.gigabytes))")
                        }
                    }
                    Spacer(minLength: 0)
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                Text(Self.summary(shares, available: available))
                    .font(.callout)
                    .foregroundStyle(over ? AnyShapeStyle(.orange) : AnyShapeStyle(.secondary))
            }
            .padding(.vertical, 2)
            .accessibilityElement(children: .combine)
        }
    }

    static func colour(_ kind: MemoryShares.Share.Kind) -> Color {
        switch kind {
        case .agent: .accentColor
        case .voiceLLM: .teal
        case .speech: .purple
        }
    }

    /// "About 34 GB of 27 GB: they may not all fit at once", "About 28 GB of 48 GB in use".
    static func summary(_ shares: MemoryShares, available: Double) -> String {
        let used = "About \(Formatting.gigabytes(shares.total)) of \(Formatting.gigabytes(available))"
        return shares.total > available ? used + ": they may not all fit at once" : used + " in use"
    }
}
