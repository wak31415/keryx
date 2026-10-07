import Foundation
import KeryxKit

/// How the Overview says what the status and the doctor report: a version as a person reads
/// it, a check's detail without its code formatting, and the restart's promise.
enum OverviewWording {
    // MARK: - Version

    /// `git describe` in words: "v0.1.0-22-ge5d742f" is "v0.1.0, 22 commits ahead", and a
    /// trailing "-dirty" adds "with uncommitted changes". Anything else is shown as it is.
    static func version(_ describe: String) -> String {
        var text = describe
        let dirty = text.hasSuffix("-dirty")
        if dirty { text.removeLast("-dirty".count) }
        var parts: [String]
        let pieces = text.split(separator: "-", omittingEmptySubsequences: false).map(String.init)
        if pieces.count >= 3, let ahead = Int(pieces[pieces.count - 2]), Self.isHash(pieces[pieces.count - 1]) {
            let tag = pieces.dropLast(2).joined(separator: "-")
            parts = [tag, ahead == 1 ? "1 commit ahead" : "\(ahead) commits ahead"]
        } else {
            parts = [text]
        }
        if dirty { parts.append("with uncommitted changes") }
        return parts.joined(separator: ", ")
    }

    /// `g` and an abbreviated commit hash, as `git describe` writes one.
    private static func isHash(_ piece: String) -> Bool {
        piece.count >= 5 && piece.hasPrefix("g") && piece.dropFirst().allSatisfy(\.isHexDigit)
    }

    // MARK: - The restart

    /// What a restart does, said without promising the call-back, which only comes when
    /// Keryx has a number to ring and a phone line to ring it on.
    static let restartMessage = "Keryx restarts and comes back in a few seconds."

    // MARK: - Doctor's details

    /// A check's detail as two parts: a path it opens with (shown on a line of its own,
    /// shortened in the middle), and the sentence. Code formatting goes, a trailing exception
    /// name ("(ConnectError)") goes, and a setting's key is replaced by its title from
    /// `titles` (`KEY` → "Title") when Configuration has named it.
    static func detail(_ raw: String, titles: [String: String] = [:]) -> (path: String?, text: String) {
        var text = raw.replacingOccurrences(of: "`", with: "").trimmingCharacters(in: .whitespaces)
        if let range = text.range(of: #"\s*\([A-Z][A-Za-z]*(Error|Exception|Timeout)\)$"#, options: .regularExpression) {
            text.removeSubrange(range)
        }
        var path: String?
        if text.hasPrefix("/") || text.hasPrefix("~/") {
            let separators = ["; ", " — ", ": "]
            let cut = separators.compactMap { text.range(of: $0) }.min { $0.lowerBound < $1.lowerBound }
            if let cut {
                path = String(text[..<cut.lowerBound])
                text = String(text[cut.upperBound...])
            } else {
                path = text
                text = ""
            }
        }
        text = Wording.named(text, titles: titles)
        return (path, Wording.capitalised(text))
    }
}
