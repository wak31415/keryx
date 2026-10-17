import KeryxKit

/// How the Overview says whether Keryx is answering calls, and its tunnel's unit. The app
/// starts and stops Keryx's unit itself, so the unit and its manager stay in a tooltip, and
/// the word "service" is never said.
enum ServiceWording {
    /// The "Answering calls" row: a dot (or a spinner while it starts or stops), a word, a
    /// line under it when there is more to say, the unit in its tooltip, and whether Start
    /// is offered — only when Keryx should run and doesn't, as the fallback for a start that
    /// failed or never ran.
    struct Answering: Equatable {
        var tone: StatusDot.Tone
        var text: String
        var hint: String?
        var help: String
        var busy = false
        var offersStart = false
        /// What Keryx printed, for Show Details only: never the line, nor its tooltip.
        var details: String?
    }

    /// What the row says while the first start waits for the last Quit's stop to finish.
    static let finishingStop = "Finishing stopping…"

    /// The row from the status, what keeping Keryx answering asks for (`step`), the start or
    /// stop under way (`change`), the last start's failure, and whether the first start is
    /// waiting for the last Quit's stop (`finishingStop`).
    static func answering(_ status: StatusReport, step: AnsweringStep, change: AppModel.ServiceChange?,
                          failure: KeryxError?, finishingStop: Bool = false) -> Answering {
        let s = status.service
        let unit = [s.manager ?? "launchd", s.unit].compactMap { $0 }.joined(separator: " ")
        if finishingStop {
            return Answering(tone: .inactive, text: Self.finishingStop,
                             help: "Keryx is still stopping from when the app last quit; it starts again once it has.",
                             busy: true)
        }
        switch change {
        case .installing?:
            return Answering(tone: .inactive, text: "Starting…", help: "Starting \(unit)", busy: true)
        case .uninstalling?:
            return Answering(tone: .inactive, text: "Stopping…", help: "Stopping \(unit)", busy: true)
        case nil:
            break
        }
        if !s.ok {
            return Answering(tone: .warning, text: "Unknown",
                             hint: s.error.flatMap { AppFacing.isFit($0) ? Wording.sentence($0) : nil },
                             help: s.error ?? "")
        }
        if s.installed && s.active { return Answering(tone: .ok, text: "Yes", help: "Running as \(unit)") }
        if status.health.reachable {
            return Answering(tone: .ok, text: "Yes", hint: "Started outside this app",
                             help: "Keryx answers, but not as \(unit): it was started by hand.")
        }
        if let failure {
            // The app's own words; what Keryx printed only under Show Details.
            let said = failure.said("Keryx couldn't start answering calls.")
            return Answering(tone: .error, text: "No", hint: "Keryx couldn't start.", help: said.message,
                             offersStart: true, details: said.detail)
        }
        switch step {
        case .start:
            // Relaunched into a Keryx the owner stopped (`keryx service stop`):
            // said quietly, never as an error, and gone as soon as it starts (the `s.active`
            // branch above returns first, once it has).
            let hint = status.restart.state == "stopped" ? "Keryx was stopped by its owner" : nil
            return Answering(tone: .inactive, text: "No", hint: hint, help: "\(unit) is not running",
                             offersStart: true)
        case .waitingForSetup:
            return Answering(tone: .inactive, text: "Not yet", hint: "Keryx starts once setup is finished",
                             help: "\(unit) starts once setup is finished")
        case .unknown, .running:
            return Answering(tone: .inactive, text: "No", help: "\(unit) is not running")
        }
    }

    /// The tunnel's unit in a word.
    static func tunnel(_ unit: UnitState) -> (tone: StatusDot.Tone, text: String) {
        switch (unit.installed, unit.active) {
        case (false, _): (.inactive, "Not installed")
        case (true, true): (.ok, "Running")
        case (true, false): (.inactive, "Not running")
        }
    }
}
