import KeryxKit

/// How the Overview's service rows say Keryx's units (`keryx service --json`), and what
/// installing or uninstalling them asks first.
enum ServiceWording {
    /// Installing or uninstalling Keryx and its tunnel: `keryx service install|uninstall`,
    /// never `--llm`/`--voice` (the model and voice servers are started on their own screens).
    enum Change: Equatable {
        case install, uninstall

        var question: String {
            switch self {
            case .install: "Install Keryx as a Service?"
            case .uninstall: "Uninstall the Keryx Service?"
            }
        }

        var button: String {
            switch self {
            case .install: "Install"
            case .uninstall: "Uninstall"
            }
        }

        /// What happens, in a sentence or two. `assistant`: "Lyra". `liveCalls`: calls in
        /// progress now, which uninstalling ends.
        func message(assistant: String, liveCalls: Int) -> String {
            switch self {
            case .install:
                return "Keryx and its tunnel start now, and again whenever you log in, so \(assistant) "
                    + "can answer the phone. launchd starts them again if they stop."
            case .uninstall:
                let calls = switch liveCalls {
                case 0: ""
                case 1: " The call in progress ends."
                default: " The \(liveCalls) calls in progress end."
                }
                return "Keryx and its tunnel stop, and no longer start when you log in, so \(assistant) "
                    + "won't answer the phone until they're installed again. Your settings and what Keryx "
                    + "remembers are kept.\(calls)"
            }
        }

        /// What is said while it runs.
        var progress: String {
            switch self {
            case .install: "Installing…"
            case .uninstall: "Uninstalling…"
            }
        }
    }

    /// The tunnel's unit in a word.
    static func tunnel(_ unit: UnitState) -> (tone: StatusDot.Tone, text: String) {
        switch (unit.installed, unit.active) {
        case (false, _): (.inactive, "Not installed")
        case (true, true): (.ok, "Running")
        case (true, false): (.warning, "Stopped")
        }
    }
}
