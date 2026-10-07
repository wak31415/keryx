/// Which of the three menu-bar images says the menu's state: the plain mark, the mark with a
/// dot (something to look at), or the mark dimmed and struck through (Keryx is not running,
/// or cannot be found). `MenuState.symbol` chooses one; `MenuBarIcon.image` draws it.
public enum MenuSymbol: Sendable, CaseIterable {
    case plain, dot, slashed
}
