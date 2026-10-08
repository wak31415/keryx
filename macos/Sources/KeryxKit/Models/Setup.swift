import Foundation

/// `keryx setup plan --json`: the steps to a first working call, in order, and whether it
/// is ready for one. What the Setup Assistant renders; it decides nothing itself.
public struct SetupPlan: Codable, Sendable, Equatable {
    /// The plan's own schema (1 today), so a renderer can tell a shape it does not know.
    public var schema: Int
    /// Every required step is done: a first call can be made.
    public var ready: Bool
    public var steps: [SetupStep]

    /// The step with this id (`voice`, `pin`, `phone`, …).
    public func step(_ id: String) -> SetupStep? { steps.first { $0.id == id } }

    /// The required steps not yet done, in order.
    public var remaining: [SetupStep] { steps.filter { $0.required && $0.kind != .done } }

    /// SEAM: the plan says which steps may be left for later (`deferrable`), as a CLI with
    /// Task L does; `false` from one without it.
    public var speaksOfDeferring: Bool { steps.contains { $0.deferrable != nil } }

    /// Everything required is done but what was left for later (`deferred`): "Nearly
    /// Ready". Always false from a CLI that cannot defer.
    public var nearlyReady: Bool {
        !ready && speaksOfDeferring && !remaining.isEmpty && remaining.allSatisfy { $0.deferred == true }
    }
}

/// One step of the plan. Its ids are the CLI's: `voice`, `agents`, `owner`, `pin`,
/// `phone`, `service` (required); `local`, `google`, `plugins`, `issues`, `profile`,
/// `projects` (optional).
public struct SetupStep: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var title: String
    public var required: Bool
    /// `done`, `needed`, `optional`, `blocked` or `failed`; see `kind`.
    public var state: String
    /// One sentence on what is left (or, for a blocked step, what it waits for); `nil` once
    /// the step is done.
    public var why: String?
    /// The ids of the steps this one waits for, while it is `blocked`.
    public var blockedBy: [String]
    /// Walked (or skipped) before, as `keryx setup mark` records it.
    public var walked: Bool
    /// The doctor's checks behind the state, each `ok`, `missing` or `failed`.
    public var checks: [SetupCheck]
    /// The app's action for the check `why` names (`FixAction`); `nil` once done, while it
    /// waits, or when the app has nothing to offer. Optional for an older CLI.
    public var fix: String?
    /// SEAM (Task L, `setup plan --json`, not printed yet): the step may be left for later
    /// (`setup mark phone|service`) — the phone and the service. `nil` from a CLI without it,
    /// which refuses to mark a required step other than the PIN.
    public var deferrable: Bool?
    /// SEAM: the owner left it for later. It keeps its state and `why`, and `ready` stays
    /// false; the steps that waited for it stop waiting.
    public var deferred: Bool?

    /// The state, when this build knows it.
    public var kind: SetupStepState? { SetupStepState(rawValue: state) }
}

/// What a setup step's `state` says.
public enum SetupStepState: String, Sendable, CaseIterable {
    case done, needed, optional, blocked, failed
}

/// One check behind a step: what `keryx doctor` said, in a step's words.
public struct SetupCheck: Codable, Sendable, Equatable {
    public var title: String
    /// `ok`, `missing` or `failed`.
    public var state: String
    public var detail: String
    /// The app's action for it, as doctor gives it (`FixAction`). Optional for an older CLI.
    public var fix: String?
}

/// `keryx setup mark STEP --json`: the step recorded as walked.
public struct SetupMarkResult: Codable, Sendable, Equatable {
    public var id: String
    public var walked: Bool
    /// SEAM (Task L, not printed yet): the step is now left for later (`setup mark phone`).
    public var deferred: Bool?

    public init(id: String, walked: Bool, deferred: Bool? = nil) {
        self.id = id
        self.walked = walked
        self.deferred = deferred
    }
}

/// `keryx pin --json`: whether a phone PIN is set and where it is kept — never the digits,
/// nor how many there are — and whether wrong guesses have locked PIN entry.
public struct PinStatus: Codable, Sendable, Equatable {
    public var set: Bool
    /// `environment`, `file` or `jarvis` (still in an unmigrated `~/.jarvis`); see `place`.
    public var source: String?
    /// No PIN anywhere, so the first call may enrol one.
    public var enrolmentOpen: Bool
    /// Wrong PINs, across calls, have locked PIN entry until `lockedUntil`.
    public var locked: Bool
    public var lockedUntil: Date?

    /// Where it is kept, when this build knows the word.
    public var place: PinSource? { source.flatMap(PinSource.init(rawValue:)) }
}

/// Where a PIN is kept.
public enum PinSource: String, Sendable, CaseIterable {
    /// `KERYX_PIN` in the environment: the app cannot change it.
    case environment
    /// `KERYX_HOME/pin`, the usual place.
    case file
    /// `~/.jarvis`, until `keryx migrate` moves it.
    case jarvis
}

/// `keryx pin set --stdin [--replace] --json`.
public struct PinSetResult: Codable, Sendable, Equatable {
    public var set: Bool
    public var replaced: Bool
    /// Keryx is running with the PIN it started with until it restarts.
    public var restartNeeded: Bool

    public init(set: Bool, replaced: Bool, restartNeeded: Bool) {
        self.set = set
        self.replaced = replaced
        self.restartNeeded = restartNeeded
    }
}

/// `keryx config verify KEY [--stdin [--pair]] --json`: whether a key works, saving
/// nothing. A key that does not work is an answer (`ok` false, `problem` saying why), not
/// a failure.
public struct VerifyResult: Codable, Sendable, Equatable {
    public var key: String
    public var ok: Bool
    /// One sentence of cause, when it does not work.
    public var problem: String?
    /// What is worth saying when it does: the Twilio account's name.
    public var detail: String?

    public init(key: String, ok: Bool, problem: String?, detail: String?) {
        self.key = key
        self.ok = ok
        self.problem = problem
        self.detail = detail
    }

    /// The keys `config verify` can check.
    public static let verifiable: Set<String> = [
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_API_KEY", "TWILIO_AUTH_TOKEN",
    ]
}
