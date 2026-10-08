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

    /// The first letter of a sentence the CLI wrote in lower case, raised — unless the
    /// sentence opens on a program's name, which is spelt as it is typed: a word with `-`,
    /// `_` or `.` in it (`llama-server`, `install-launchd.sh`), or one of `programs`.
    public static func capitalised(_ text: String) -> String {
        let first = text.prefix { !$0.isWhitespace && $0 != ":" && $0 != "," }
        if first.contains(where: { "-_.".contains($0) }) || programs.contains(String(first)) { return text }
        return text.prefix(1).uppercased() + text.dropFirst()
    }

    /// Programs a sentence may open on, whose names are lower case wherever they appear.
    static let programs: Set<String> = [
        "brew", "cloudflared", "gh", "git", "keryx", "launchctl", "ngrok", "ollama", "pip", "python3", "uv",
    ]

    /// `text` with every setting key in `titles` (`ALLOWED_CALLERS`) replaced by its title in
    /// quotation marks (“Your phone numbers”), so a sentence written about the key reads
    /// right whatever the title's shape. Keys `titles` does not name stay as they are.
    public static func named(_ text: String, titles: [String: String]) -> String {
        guard !titles.isEmpty else { return text }
        var result = ""
        var rest = Substring(text)
        while let match = rest.range(of: #"\b[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\b"#, options: .regularExpression) {
            result += rest[..<match.lowerBound]
            let key = String(rest[match])
            result += titles[key].map { "“\($0)”" } ?? key
            rest = rest[match.upperBound...]
        }
        return result + rest
    }

    /// Each setting's title by its key, from Configuration's rows (an older CLI's rows have
    /// none, and their keys stay).
    public static func titles(_ rows: [ConfigRow]?) -> [String: String] {
        var titles: [String: String] = [:]
        for row in rows ?? [] {
            if let title = row.title, !title.isEmpty { titles[row.key] = row.shownTitle }
        }
        return titles
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

    /// "Voice, Coding Agents and Phone".
    public static func list(_ items: [String]) -> String {
        guard let last = items.last else { return "" }
        guard items.count > 1 else { return last }
        return items.dropLast().joined(separator: ", ") + " and " + last
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
