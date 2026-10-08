import KeryxKit

/// How the Overview says whether Keryx is answering calls, and its tunnel's unit. The app
/// starts and stops Keryx's unit itself, so the unit, its manager and the word "service"
/// stay in a tooltip.
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
    }

    /// The row from the status, what keeping Keryx answering asks for (`step`), the start or
    /// stop under way (`change`) and the last start's failure.
    static func answering(_ status: StatusReport, step: AnsweringStep, change: AppModel.ServiceChange?,
                          failure: KeryxError?) -> Answering {
        let s = status.service
        let unit = [s.manager ?? "launchd", s.unit].compactMap { $0 }.joined(separator: " ")
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
            return Answering(tone: .error, text: "No", hint: Self.couldNotStart(failure),
                             help: failure.message, offersStart: true)
        }
        switch step {
        case .start:
            return Answering(tone: .inactive, text: "No", help: "\(unit) is not running", offersStart: true)
        case .waitingForSetup:
            return Answering(tone: .inactive, text: "Not yet", hint: "Keryx starts once setup is finished",
                             help: "\(unit) starts once setup is finished")
        case .unknown, .running:
            return Answering(tone: .inactive, text: "No", help: "\(unit) is not running")
        }
    }

    /// The line under "No" when the start failed: Keryx's own sentence when it is fit to
    /// show, else only that it could not start (the tooltip has the rest).
    static func couldNotStart(_ failure: KeryxError) -> String {
        let message = failure.message
        return AppFacing.isFit(message) ? "Keryx couldn't start: \(Wording.sentence(message))" : "Keryx couldn't start."
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
