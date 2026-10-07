import AppKit

/// What quitting asks while something is still running that quitting would stop.
public enum Quitting {
    public struct Question: Equatable, Sendable {
        public let message: String
        public let detail: String
    }

    /// The question for a download under way, a voice setup under way, or both; `nil` when
    /// neither is and the app can just quit. `assistant`: the assistant's name ("Lyra").
    public static func question(downloading: Bool, settingUpVoice: Bool, assistant: String?) -> Question? {
        let voice = "\(possessive(assistant, opening: true)) voice is being set up"
        switch (downloading, settingUpVoice) {
        case (false, false):
            return nil
        case (true, false):
            return Question(message: "A model is downloading. Quit anyway?",
                            detail: "It will pick up where it left off next time.")
        case (false, true):
            return Question(message: "\(voice). Quit anyway?",
                            detail: "The setup stops. Set Up… in Voice starts it again.")
        case (true, true):
            return Question(message: "A model is downloading and \(possessive(assistant, opening: false)) voice is being set up. Quit anyway?",
                            detail: "Both stop. The download picks up where it left off, and Set Up… in Voice "
                                + "starts the setup again.")
        }
    }

    /// "Lyra's", or "the assistant's" before a name is known ("The" opening a sentence).
    private static func possessive(_ name: String?, opening: Bool) -> String {
        guard let name, !name.isEmpty else { return opening ? "The assistant's" : "the assistant's" }
        return name + "'s"
    }
}

/// Which menus opening should refresh what the menu bar lists.
@MainActor public enum MenuTracking {
    /// `menu` is the menu of a pop-up button (a `Picker` in one of the windows): opening it
    /// is choosing a value, not looking at the menu bar.
    public static func isPopUpMenu(_ menu: NSMenu, in windows: [NSWindow]) -> Bool {
        windows.contains { window in
            guard let view = window.contentView else { return false }
            return contains(view, menu)
        }
    }

    private static func contains(_ view: NSView, _ menu: NSMenu) -> Bool {
        if let button = view as? NSPopUpButton, button.menu === menu { return true }
        return view.subviews.contains { contains($0, menu) }
    }
}
