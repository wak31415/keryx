import Foundation
import KeryxKit

/// Names the app shows for the CLI's identifiers, so every screen says them the same way.
public enum Wording {
    /// A coding agent's name as the owner knows it: `claude` → "Claude", `local` →
    /// "Local model". An agent this build does not know is shown as the CLI names it.
    public static func agent(_ name: String) -> String {
        switch name {
        case "claude": "Claude"
        case "codex": "Codex"
        case "local": "Local model"
        default: name
        }
    }

    /// The assistant's name, once the status has said it.
    public static func assistant(_ status: StatusReport?) -> String? {
        guard let name = status?.assistant.name, !name.isEmpty else { return nil }
        return name
    }

    /// The menu state in a few lower-case words, for the window's subtitle ("Lyra · running").
    /// `nil` while Keryx is still being looked for.
    public static func shortStatus(_ state: MenuState) -> String? {
        switch state {
        case .locating: nil
        case .notFound: "not found"
        case .needsMigration: "needs to move its files"
        case .stopped: "stopped"
        case .notInstalled: "not installed"
        case .idle: "running"
        case let .working(count): count == 1 ? "1 session running" : "\(count) sessions running"
        case let .attention(reason): reason.prefix(1).lowercased() + reason.dropFirst()
        }
    }

    /// The first letter of a sentence the CLI wrote in lower case, raised.
    public static func capitalised(_ text: String) -> String {
        text.prefix(1).uppercased() + text.dropFirst()
    }

    /// A heading in title case, as the CLI's sentence-case group titles become section
    /// titles: "Local and self-hosted models" → "Local and Self-Hosted Models". Short joining
    /// words stay lower case after the first; a word already capitalised (PIN) is kept.
    public static func titleCase(_ text: String) -> String {
        let minor: Set<String> = ["a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to"]
        let words = text.split(separator: " ", omittingEmptySubsequences: false).map(String.init)
        return words.enumerated().map { index, word in
            if index > 0, minor.contains(word) { return word }
            return word.split(separator: "-", omittingEmptySubsequences: false)
                .map { capitalised(String($0)) }.joined(separator: "-")
        }.joined(separator: " ")
    }

    /// `text` as a sentence: a capital first letter and a closing full stop.
    public static func sentence(_ text: String) -> String {
        KeryxError.sentence(text)
    }

    /// `text` cut to `limit` characters, ending in an ellipsis when it was cut.
    public static func truncated(_ text: String, to limit: Int) -> String {
        guard text.count > limit else { return text }
        return text.prefix(limit - 1).trimmingCharacters(in: .whitespaces) + "…"
    }
}
