import Foundation
import KeryxKit

/// What the PIN sheet says of `keryx pin --json`, what it lets the owner do, and what it
/// checks in the two fields before the digits go to `keryx pin set --stdin`. Never the
/// digits themselves: nothing here keeps, shows or logs them.
package enum PinWording {
    /// Where the PIN is, as far as setting one goes.
    package enum Kind: Equatable, Sendable {
        /// No PIN anywhere.
        case none
        /// In `KERYX_HOME/pin`.
        case set
        /// `KERYX_HOME/pin` is there and holds no usable PIN: none, and no call can set one.
        case unusable
        /// `KERYX_PIN` in the environment, which wins; `usable` false when it isn't 6-8 digits.
        case environment(usable: Bool)
        /// Still in an unmigrated `~/.jarvis`.
        case jarvis
        /// A place this build does not know.
        case unknown
    }

    package static func kind(_ status: PinStatus) -> Kind {
        switch status.place {
        case .environment: .environment(usable: status.set)
        case .jarvis: .jarvis
        case .file: status.set ? .set : .unusable
        case nil: status.source == nil ? .none : .unknown
        }
    }

    /// The app may write a PIN: there is none, or one in `KERYX_HOME/pin`. Not past the
    /// environment's, which would win over it, nor beside one still in `~/.jarvis`.
    package static func canSet(_ status: PinStatus) -> Bool {
        [.none, .set, .unusable].contains(kind(status))
    }

    /// The write puts a new PIN in place of what `KERYX_HOME/pin` holds (`--replace`).
    package static func replaces(_ status: PinStatus) -> Bool {
        [.set, .unusable].contains(kind(status))
    }

    /// Replacing a working PIN asks "Are you sure?" before the fields are shown, and the
    /// digits are then typed twice: the two yeses `keryx setup` asks for. A file with no
    /// usable PIN in it has nothing to lose, as in `keryx setup`.
    package static func asksFirst(_ status: PinStatus) -> Bool {
        kind(status) == .set
    }

    /// The button that opens the sheet.
    package static func buttonTitle(_ status: PinStatus?, fallbackSet: Bool = false) -> String {
        (status?.set ?? fallbackSet) ? "Change PIN…" : "Set PIN…"
    }

    /// The sheet's title.
    package static func sheetTitle(_ status: PinStatus) -> String {
        guard canSet(status) else { return "Phone PIN" }
        return asksFirst(status) ? "Change Phone PIN" : "Set Phone PIN"
    }

    /// The sheet's primary button.
    package static func actionTitle(_ status: PinStatus) -> String {
        replaces(status) && kind(status) == .set ? "Change PIN" : "Set PIN"
    }

    // MARK: - The banner

    package enum Tone: Equatable, Sendable {
        case ok, attention, blocked
    }

    /// The state at the top of the sheet: a symbol, a title and a sentence or two.
    package struct Banner: Equatable, Sendable {
        package let symbol: String
        package let tone: Tone
        package let title: String
        package let text: String
    }

    /// `assistant`: the assistant's name ("Lyra"), when the status has said it.
    package static func banner(_ status: PinStatus, assistant: String?) -> Banner {
        let name = assistant ?? "the assistant"
        switch kind(status) {
        case .none:
            let first = status.enrolmentOpen
                ? "The first call may set one: whoever calls first chooses it."
                : "No call can set one."
            return Banner(symbol: "lock.open", tone: .attention, title: "No PIN Is Set",
                          text: "\(first) Until a PIN exists, \(name) reads nothing of yours out on the phone "
                              + "and does nothing a caller asks.")
        case .set:
            return Banner(symbol: "lock.fill", tone: .ok, title: "A PIN Is Set",
                          text: "Callers key it, or say it, before \(name) does anything they ask. "
                              + "It's kept in KERYX_HOME/pin, readable only by you.")
        case .unusable:
            return Banner(symbol: "lock.trianglebadge.exclamationmark", tone: .attention,
                          title: "The PIN File Holds No PIN",
                          text: "KERYX_HOME/pin is there but isn't a PIN, so there is none, and no call can set one. "
                              + "A PIN set here takes its place.")
        case .environment(usable: true):
            return Banner(symbol: "lock.fill", tone: .ok, title: "Set in the Environment",
                          text: "KERYX_PIN is set where Keryx runs, and it wins over a PIN set here. "
                              + "Change it there, or remove it to set one here.")
        case .environment(usable: false):
            return Banner(symbol: "lock.trianglebadge.exclamationmark", tone: .blocked,
                          title: "The Environment's PIN Doesn't Work",
                          text: "KERYX_PIN is set where Keryx runs but isn't 6 to 8 digits, so the phone can't use it "
                              + "and Keryx won't start with it. Fix it there, or remove it to set one here.")
        case .jarvis:
            return Banner(symbol: "shippingbox", tone: .attention, title: "Still Where Jarvis Kept It",
                          text: "The PIN is still in ~/.jarvis. Once Keryx's files have moved (keryx migrate), "
                              + "it can be changed here.")
        case .unknown:
            return Banner(symbol: "questionmark.circle", tone: .attention, title: "Kept Somewhere New",
                          text: "Keryx keeps this PIN somewhere this app doesn't know. Update the app to change it here.")
        }
    }

    /// The wrong-PIN lockout, when it holds. Nothing lifts it early, a new PIN included.
    package static func lockLine(_ status: PinStatus, now: Date = .now) -> String? {
        guard status.locked else { return nil }
        let until = status.lockedUntil.map { " until \(time($0, now: now))" } ?? ""
        return "Too many wrong PINs: PIN entry on calls is locked\(until). A new PIN doesn't lift the lock."
    }

    /// The time alone today, else the date and the time.
    static func time(_ date: Date, now: Date) -> String {
        Calendar.current.isDate(date, inSameDayAs: now)
            ? date.formatted(date: .omitted, time: .shortened)
            : date.formatted(.dateTime.month(.abbreviated).day().hour().minute())
    }

    /// Configuration's line under the PIN's button. `fallbackSet`: the row's own word, until
    /// `keryx pin` has answered.
    package static func caption(_ status: PinStatus?, fallbackSet: Bool, now: Date = .now) -> String {
        guard let status else { return fallbackSet ? "Set" : "Not set" }
        let place: String = switch kind(status) {
        case .none: "Not set"
        case .set: "Set · in KERYX_HOME/pin"
        case .unusable: "No usable PIN"
        case .environment(usable: true): "Set in the environment"
        case .environment(usable: false): "The environment's PIN doesn't work"
        case .jarvis: "Set · still in ~/.jarvis"
        case .unknown: status.set ? "Set" : "Not set"
        }
        guard status.locked else { return place }
        let until = status.lockedUntil.map { " until \(time($0, now: now))" } ?? ""
        return "\(place) · locked\(until)"
    }

    // MARK: - The two fields

    /// What the CLI holds a PIN chosen at the keyboard to (`setup/pin.py`), in its words.
    package static let rule = "6 to 8 digits. Not a run like 123456, nor one digit repeated."

    /// What is wrong with what is typed, or `nil` while nothing is (or nothing yet worth
    /// saying). `submitted`: the owner has asked to save, so a PIN too short is said too.
    package static func problem(first: String, second: String, submitted: Bool = false) -> String? {
        if first.isEmpty { return submitted ? "Type the new PIN." : nil }
        if !first.allSatisfy(isDigit) { return "Use digits only." }
        if first.count > 8 { return "Use 6 to 8 digits." }
        let typedOut = submitted || !second.isEmpty
        if first.count < 6 { return typedOut ? "Use 6 to 8 digits." : nil }
        if trivial(first) { return "Pick something less guessable than a run or a repeated digit." }
        if second.isEmpty { return submitted ? "Type the PIN again to confirm it." : nil }
        if second != first, submitted || second.count >= first.count { return "The two PINs differ." }
        return nil
    }

    /// Both fields hold the same usable PIN.
    package static func ready(first: String, second: String) -> Bool {
        problem(first: first, second: second, submitted: true) == nil
    }

    /// An ASCII digit: a PIN in another script's digits is one no keypad can enter.
    private static func isDigit(_ character: Character) -> Bool {
        character.isASCII && character.isNumber
    }

    /// One digit repeated, or a straight run up or down: `config.is_trivial_pin`.
    static func trivial(_ digits: String) -> Bool {
        let values = digits.compactMap(\.wholeNumberValue)
        guard values.count == digits.count, values.count > 1 else { return false }
        if Set(values).count == 1 { return true }
        let steps = Set(zip(values, values.dropFirst()).map { $1 - $0 })
        return steps == [1] || steps == [-1]
    }

    // MARK: - What went wrong

    /// Where a refusal belongs, and its sentence. Exit 2 is the digits not doing (they
    /// differ, or are not a usable PIN): said under the fields. Exit 1 is this Mac not
    /// taking a PIN now (one is already set, the environment's wins, it is still in
    /// `~/.jarvis`, it could not be written): said above them.
    package static func failure(_ error: KeryxError) -> (aboutTheDigits: Bool, text: String) {
        if case let .failed(_, exitCode, message) = error, !message.isEmpty {
            return (exitCode == 2, KeryxError.sentence(message))
        }
        return (false, error.message)
    }

    /// What the sheet says once the PIN is saved.
    package static func saved(replaced: Bool, restartNeeded: Bool) -> String {
        let done = replaced ? "The PIN is changed." : "The PIN is set."
        guard restartNeeded else { return done }
        return "\(done) Keryx is running with the PIN it started with until it restarts."
    }
}
