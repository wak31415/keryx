import KeryxKit
import SwiftUI

/// How the Setup Assistant says a step's state, draws a step, and words its last page.
enum SetupWording {
    // MARK: - States

    /// The symbol beside a step in the list: a green check once done, an empty circle while
    /// it is needed, a dotted one while it is optional, a clock while it waits, and a red
    /// exclamation mark when a check failed. A state this build does not know is a dashed
    /// circle.
    static func symbol(_ state: SetupStepState?) -> String {
        switch state {
        case .done?: "checkmark.circle.fill"
        case .needed?: "circle"
        case .optional?: "circle.dotted"
        case .blocked?: "clock"
        case .failed?: "exclamationmark.circle.fill"
        case nil: "circle.dashed"
        }
    }

    static func colour(_ state: SetupStepState?) -> Color {
        switch state {
        case .done?: .green
        case .needed?: .accentColor
        case .failed?: .red
        case .optional?, .blocked?, nil: .secondary
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
    /// in orange, what failed in red, a clock for what it waits for.
    static func statusSymbol(_ state: SetupStepState?) -> (name: String, colour: Color) {
        switch state {
        case .done?: ("checkmark.circle.fill", .green)
        case .needed?: ("exclamationmark.circle", .orange)
        case .failed?: ("exclamationmark.circle.fill", .red)
        case .blocked?: ("clock", .secondary)
        case .optional?, nil: ("info.circle", .secondary)
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
    static let pinFromEnvironment = "The PIN comes from KERYX_PIN in Keryx's environment, so it's set where that is."

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
