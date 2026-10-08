import Foundation

/// How the setup wizard walks its frames.
public enum SetupWizardMode: Sendable, Equatable {
    /// At launch, and from the Overview's checklist: what is set already is folded or skipped.
    case firstRun
    /// From "Run Setup Wizard…": every frame, prefilled, Continue meaning keep.
    case review
}

/// One request to open the setup wizard: its mode, and an id of its own so the window, if
/// it is open already, starts again at Welcome.
public struct WizardOpening: Sendable, Equatable {
    public var mode: SetupWizardMode
    public var id: UUID

    public init(mode: SetupWizardMode, id: UUID = UUID()) {
        self.mode = mode
        self.id = id
    }
}
