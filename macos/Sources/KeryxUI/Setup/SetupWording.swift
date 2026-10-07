import KeryxKit
import SwiftUI

/// How the Setup Assistant says a step's state, draws a step, and words its last page.
enum SetupWording {
    // MARK: - States

    /// The symbol beside a step in the list: a green check once done, a grey ring while a
    /// required step is still to do, a dotted grey ring for an optional one left off, a clock
    /// while it waits, and the doctor's red failure mark when a check failed — the same
    /// glyphs as the Overview's checks (`CheckGlyph`). A state this build does not know is a
    /// dashed circle.
    static func symbol(_ state: SetupStepState?) -> String {
        switch state {
        case .done?: CheckGlyph.ok.symbol
        case .needed?: "circle"
        case .optional?: "circle.dotted"
        case .blocked?: "clock"
        case .failed?: CheckGlyph.failed.symbol
        case nil: "circle.dashed"
        }
    }

    static func colour(_ state: SetupStepState?) -> Color {
        switch state {
        case .done?: CheckGlyph.ok.colour
        case .failed?: CheckGlyph.failed.colour
        case .needed?, .optional?, .blocked?, nil: .secondary
        }
    }

    /// What VoiceOver says of the state.
    static func label(_ state: SetupStepState?) -> String {
        switch state {
        case .done?: "Done"
        case .needed?: "Needed"
        case .optional?: "Optional"
        case .blocked?: "Waiting"
        case .failed?: "Not working"
        case nil: "Unknown"
        }
    }

    /// The symbol before the line on a step's page that says what is left: what is missing
    /// with the doctor's attention mark, what failed with its failure mark, a clock for what
    /// it waits for.
    static func statusSymbol(_ state: SetupStepState?) -> (name: String, colour: Color) {
        switch state {
        case .done?: (CheckGlyph.ok.symbol, CheckGlyph.ok.colour)
        case .needed?: (CheckGlyph.missing.symbol, CheckGlyph.missing.colour)
        case .failed?: (CheckGlyph.failed.symbol, CheckGlyph.failed.colour)
        case .blocked?: ("clock", .secondary)
        case .optional?, nil: ("info.circle", .secondary)
        }
    }

    /// The line under a step's title, as a plain sentence: what is left in the words of what
    /// would mend it ("No OpenAI API key yet.", "Claude isn't signed in."), what it waits
    /// for, or that it is done. The plan's own `why` (a check's title and its state) only
    /// when the fix says nothing better; `nil` for an optional step's offer, which the page's
    /// own sentence already makes.
    static func headline(_ step: SetupStep, in plan: SetupPlan, context: SetupContext) -> String? {
        switch step.kind {
        case .done?:
            return done
        case .blocked?:
            return waitsFor(step, in: plan)
        default:
            guard let why = why(step) else { return nil }
            if let fix = FixAction(step.fix),
               let line = sentence(fix, failed: step.kind == .failed, context: context) { return line }
            return Wording.named(why, titles: Wording.titles(context.config))
        }
    }

    /// "Waits for Voice, Coding Agents and Phone.": the steps a blocked step waits for, by
    /// their titles as the list shows them; the plan's own sentence when none is left open.
    static func waitsFor(_ step: SetupStep, in plan: SetupPlan) -> String? {
        let waiting = SetupGuide.blockers(of: step, in: plan).map { Wording.titleCase($0.title) }
        guard !waiting.isEmpty else { return step.why }
        return "Waits for \(Wording.list(waiting))."
    }

    /// What is wrong, as the fix for it says it: `nil` where the fix alone cannot tell (a
    /// setting, or a secret that is set but in the wrong place).
    static func sentence(_ fix: FixAction, failed: Bool, context: SetupContext) -> String? {
        switch fix {
        case let .signIn(provider):
            return "\(SignInWording.name(provider)) isn't signed in."
        case let .secret(key):
            guard !failed else { return nil }
            let title = context.row(key)?.displayTitle ?? FixAction.humanised(key)
            return "No \(title) yet."
        case .setting:
            return nil
        case let .plugin(name):
            guard !failed else { return nil }
            return "\(context.pluginTitle(name) ?? "The plugin") is off."
        case let .integration(id):
            let title = context.integrationTitle(id) ?? FixAction.integrationNames[id] ?? FixAction.humanised(id)
            return "\(title) isn't connected."
        case .pin:
            return failed ? "The PIN file holds no usable PIN." : "No PIN yet."
        case .installService:
            return "Keryx isn't running in the background yet."
        case .startModelServer:
            return "The model server isn't running."
        case .chooseModel:
            return "No local model is chosen yet."
        case .voiceHere:
            return failed ? "The voice server on this Mac isn't answering." : "The voice server on this Mac isn't set up yet."
        case .voiceDownload:
            return "The speech models aren't downloaded yet."
        case .phoneAccount:
            return failed ? "Twilio didn't accept the account." : "Twilio isn't connected yet."
        case .phoneWebhooks:
            return "Your Twilio number's calls go elsewhere."
        case .phoneTunnel:
            return context.tunnelDownload != nil ? "No tunnel is installed yet." : "Twilio can't reach this Mac yet."
        case .migrate:
            return "Jarvis's files still need to move."
        }
    }

    /// The large symbol at the top of a step's page.
    static func pageSymbol(_ id: String) -> String {
        switch id {
        case "migrate": "shippingbox"
        case "voice": "waveform"
        case "agents": "cpu"
        case "owner": "person.crop.circle"
        case "pin": "lock"
        case "phone": "phone"
        case "service": "gearshape.2"
        case "local": "desktopcomputer"
        case "google": "envelope"
        case "plugins": "puzzlepiece.extension"
        case "issues": "ladybug"
        case "profile": "person.text.rectangle"
        case "projects": "folder"
        default: "checklist"
        }
    }

    /// What a step is for, in a sentence or two under its title: the assistant's own name
    /// where it is the one acting (`assistant`, "Lyra"; "the assistant" before a name is
    /// known). `nil` for a step this build does not know.
    static func about(_ id: String, assistant: String?) -> String? {
        let name = assistant ?? "the assistant"
        let opening = assistant ?? "The assistant"
        return switch id {
        case "migrate": "Keryx used to be called Jarvis. Its files move to Keryx's folders before anything else is set up."
        case "voice": "How \(name) speaks on a call: OpenAI's Realtime voice with an API key, or a voice server of your own."
        case "agents": "The coding agent that does the work you ask for on a call. Sign in once and Keryx runs it for you."
        case "owner": "Your name, and the phones you call from. Keryx answers only these numbers."
        case "pin": "Digits you key in on a call before \(name) acts for you. Set them here, or choose them on your first call."
        case "phone": "A Twilio number to call, and a tunnel so that Twilio can reach this Mac."
        case "service": "Keryx runs in the background, starts at login, and restarts if it stops."
        case "local": "The voice and the coding agent can run on this Mac's own models, with no API key."
        case "google": "Lets the coding agents read your Gmail and Calendar."
        case "plugins": "Extra tools \(name) can use on a call: Slack, email answers, billing and cluster stats."
        case "issues": "Keryx can file its own bug reports, and your ideas for it, on GitHub."
        case "profile": "A first memory: what \(name) should know about you before your first call."
        case "projects": "\(opening) can be handed a short brief of each of your projects, written by an agent."
        default: nil
        }
    }

    /// A step's title as a page's heading: "Coding agents" → "Coding Agents".
    static func title(_ step: SetupStep) -> String {
        Wording.titleCase(step.title)
    }

    /// The plan's `why` for a step's page, unless it is only the plan's offer of an optional
    /// step ("Optional: …", "Off until you choose: …"), which the page's own sentence
    /// (`about`) already makes.
    static func why(_ step: SetupStep) -> String? {
        guard let why = step.why else { return nil }
        if step.kind == .optional, about(step.id, assistant: nil) != nil,
           offers.contains(where: why.hasPrefix) { return nil }
        return why
    }

    /// How `keryx setup plan` opens an optional step's offer (`Step.offer` in
    /// `keryx/setup/plan.py`).
    static let offers = ["Optional:", "Off until you choose:"]

    /// The line under a done step's title.
    static let done = "Done. Continue when you're ready."

    /// What a page says about the PIN while it comes from the environment, which the app
    /// cannot change.
    static let pinFromEnvironment = "The PIN is set in the environment Keryx runs in, so it's changed there, not here."

    // MARK: - The last page

    /// "You're Ready", or how far there is to go.
    static func finishTitle(_ plan: SetupPlan) -> String {
        plan.ready ? "You're Ready" : "Not Ready Yet"
    }

    /// "Lyra can take your first call. Ring +15550100 from one of your phones."
    static func finishMessage(_ plan: SetupPlan, assistant: String?, number: String?) -> String {
        guard plan.ready else {
            let left = plan.remaining.count
            return left == 1
                ? "1 required step is left before the first call."
                : "\(left) required steps are left before the first call."
        }
        let who = assistant.map { "\($0) can" } ?? "The assistant can"
        let ring = number.map { "Ring \($0) from one of your phones." } ?? "Ring your Twilio number from one of your phones."
        return "\(who) take your first call. \(ring)"
    }

    /// The heading over the steps the last page lists.
    static func finishListTitle(_ plan: SetupPlan) -> String {
        plan.ready ? "Optional, Whenever You Like" : "Still to Do"
    }

    /// The steps the last page lists: the required ones left, or once ready, the optional
    /// ones still open.
    static func finishList(_ plan: SetupPlan) -> [SetupStep] {
        plan.ready ? plan.steps.filter { !$0.required && $0.kind != .done } : plan.remaining
    }
}
