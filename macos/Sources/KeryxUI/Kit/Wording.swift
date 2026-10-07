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
        case .needsMigration: "needs keryx migrate"
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

    /// `text` cut to `limit` characters, ending in an ellipsis when it was cut.
    public static func truncated(_ text: String, to limit: Int) -> String {
        guard text.count > limit else { return text }
        return text.prefix(limit - 1).trimmingCharacters(in: .whitespaces) + "…"
    }
}
