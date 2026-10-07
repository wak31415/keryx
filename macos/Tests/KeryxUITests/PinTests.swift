import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// The PIN sheet: what it says of `keryx pin --json`, what it lets the owner do, what it
/// checks before the digits leave the app, and where a refusal is said.
@Suite struct PinWordingTests {
    func status(_ source: String?, set: Bool, open: Bool = false, locked: Date? = nil) -> PinStatus {
        var status = PreviewData.pinFile
        status.source = source
        status.set = set
        status.enrolmentOpen = open
        status.locked = locked != nil
        status.lockedUntil = locked
        return status
    }

    @Test func eachPlaceIsItsOwnKind() {
        #expect(PinWording.kind(PreviewData.pinNone) == .none)
        #expect(PinWording.kind(PreviewData.pinFile) == .set)
        #expect(PinWording.kind(status("file", set: false)) == .unusable)
        #expect(PinWording.kind(PreviewData.pinEnvironment) == .environment(usable: true))
        #expect(PinWording.kind(status("environment", set: false)) == .environment(usable: false))
        #expect(PinWording.kind(status("jarvis", set: true)) == .jarvis)
        #expect(PinWording.kind(status("keychain", set: true)) == .unknown)
    }

    @Test func theAppWritesOnlyWhereTheCLIWould() {
        #expect(PinWording.canSet(PreviewData.pinNone))
        #expect(PinWording.canSet(PreviewData.pinFile))
        #expect(PinWording.canSet(status("file", set: false)))
        #expect(!PinWording.canSet(PreviewData.pinEnvironment), "the environment's PIN wins over one set here")
        #expect(!PinWording.canSet(status("environment", set: false)))
        #expect(!PinWording.canSet(status("jarvis", set: true)), "keryx migrate moves it first")
        #expect(!PinWording.canSet(status("keychain", set: true)))
    }

    @Test func replacingAWorkingPinAsksFirstAndAnUnusableFileDoesNot() {
        #expect(PinWording.asksFirst(PreviewData.pinFile))
        #expect(PinWording.replaces(PreviewData.pinFile))
        #expect(!PinWording.asksFirst(status("file", set: false)), "no PIN to lose, as in keryx setup")
        #expect(PinWording.replaces(status("file", set: false)), "--replace puts one in its place")
        #expect(!PinWording.asksFirst(PreviewData.pinNone))
        #expect(!PinWording.replaces(PreviewData.pinNone), "a first PIN is the O_EXCL door, never --replace")
    }

    @Test func theButtonsSayWhatTheyDo() {
        #expect(PinWording.buttonTitle(PreviewData.pinNone) == "Set PIN…")
        #expect(PinWording.buttonTitle(PreviewData.pinFile) == "Change PIN…")
        #expect(PinWording.buttonTitle(nil, fallbackSet: true) == "Change PIN…")
        #expect(PinWording.actionTitle(PreviewData.pinFile) == "Change PIN")
        #expect(PinWording.actionTitle(status("file", set: false)) == "Set PIN")
        #expect(PinWording.sheetTitle(PreviewData.pinNone) == "Set Phone PIN")
        #expect(PinWording.sheetTitle(PreviewData.pinEnvironment) == "Phone PIN")
    }

    @Test func theBannerNamesTheAssistantAndThePlace() {
        let none = PinWording.banner(status(nil, set: false, open: true), assistant: "Lyra")
        #expect(none.title == "No PIN Is Set")
        #expect(none.text.hasPrefix("The first call may set one"))
        #expect(none.text.contains("Lyra reads nothing of yours"))
        #expect(PinWording.banner(status(nil, set: false, open: false), assistant: nil).text.hasPrefix("No call can set one."))
        #expect(PinWording.banner(status("environment", set: false), assistant: "Lyra").tone == .blocked)
        #expect(PinWording.banner(status("jarvis", set: true), assistant: "Lyra").text.contains("where Jarvis kept it"))
        // No setting's name, path or command is ever the banner's sentence: the variable is
        // for the terminal, under Show Details.
        let places: [PinStatus] = [status("file", set: true), status("file", set: false), status("environment", set: true),
                                   status("environment", set: false), status("jarvis", set: true)]
        for place in places {
            let banner = PinWording.banner(place, assistant: "Lyra")
            #expect(AppFacing.isFit(banner.text) && !banner.text.contains("KERYX_"), "\(banner.text)")
        }
        #expect(PinWording.banner(status("environment", set: true), assistant: nil).terminal?.contains("KERYX_PIN") == true)
    }

    @Test func theLockoutIsSaidAndNeverPromisedToLift() {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let locked = status("file", set: true, locked: now.addingTimeInterval(600))
        let line = try! #require(PinWording.lockLine(locked, now: now))
        #expect(line.hasPrefix("Too many wrong PINs: PIN entry on calls is locked until "))
        #expect(line.hasSuffix("A new PIN doesn't lift the lock."))
        #expect(PinWording.lockLine(PreviewData.pinFile) == nil)
        #expect(PinWording.caption(locked, fallbackSet: true, now: now).hasPrefix("Set · locked until "))
    }

    @Test func theCaptionFallsBackToTheRowUntilKeryxAnswers() {
        #expect(PinWording.caption(nil, fallbackSet: false) == "Not set")
        #expect(PinWording.caption(PreviewData.pinNone, fallbackSet: true) == "Not set")
        #expect(PinWording.caption(PreviewData.pinEnvironment, fallbackSet: false) == "Set · Overridden by the environment")
        #expect(PinWording.caption(status("file", set: false), fallbackSet: true) == "No usable PIN")
    }

    @Test func theDigitsAreCheckedHereAsTheCLIChecksThem() {
        #expect(PinWording.problem(first: "", second: "") == nil, "nothing typed, nothing said")
        #expect(PinWording.problem(first: "4829", second: "") == nil, "still typing")
        #expect(PinWording.problem(first: "4829", second: "4") == "Use 6 to 8 digits.")
        #expect(PinWording.problem(first: "48a915", second: "") == "Use digits only.")
        #expect(PinWording.problem(first: "٤٨٢٩١٥", second: "") == "Use digits only.", "a keypad has ASCII digits")
        #expect(PinWording.problem(first: "482915123", second: "") == "Use 6 to 8 digits.")
        #expect(PinWording.problem(first: "123456", second: "")?.hasPrefix("Pick something less guessable") == true)
        #expect(PinWording.problem(first: "987654", second: "")?.hasPrefix("Pick something") == true)
        #expect(PinWording.problem(first: "777777", second: "")?.hasPrefix("Pick something") == true)
        #expect(PinWording.problem(first: "482915", second: "4829") == nil, "the second is still being typed")
        #expect(PinWording.problem(first: "482915", second: "482916") == "The two PINs differ.")
        #expect(PinWording.problem(first: "482915", second: "4829", submitted: true) == "The two PINs differ.")
        #expect(PinWording.problem(first: "482915", second: "", submitted: true) == "Type the PIN again to confirm it.")
        #expect(PinWording.ready(first: "482915", second: "482915"))
        #expect(PinWording.ready(first: "48291573", second: "48291573"))
        #expect(!PinWording.ready(first: "482915", second: "482916"))
    }

    @Test func replacingAsksASecondTimeAfterTheDigitsAndBeforeTheyGo() {
        let file = PreviewData.pinFile
        #expect(PinWording.saveStep(file, first: "482915", second: "48291", agreed: false) == .wait)
        #expect(PinWording.saveStep(file, first: "123456", second: "123456", agreed: true) == .wait,
                "a yes never sends digits that won't do")
        #expect(PinWording.saveStep(file, first: "482915", second: "482915", agreed: false) == .confirm)
        #expect(PinWording.saveStep(file, first: "482915", second: "482915", agreed: true) == .save(replace: true))
        let unusable = status("file", set: false)
        #expect(PinWording.saveStep(unusable, first: "482915", second: "482915", agreed: false) == .confirm,
                "whatever KERYX_HOME/pin holds is asked about before it is replaced")
        #expect(PinWording.saveStep(PreviewData.pinNone, first: "482915", second: "482915", agreed: false)
            == .save(replace: false), "a first PIN replaces nothing")
        #expect(PinWording.replaceTitle == "Replace the PIN?")
    }

        @Test func aRefusalIsSaidWhereItBelongs() {
        let differ = PinWording.failure(.failed(command: "keryx pin set", exitCode: 2, message: "The two PINs differ."))
        #expect(differ.aboutTheDigits)
        #expect(differ.text == "The two PINs differ.")
        let already = PinWording.failure(.failed(command: "keryx pin set", exitCode: 1, message: "A PIN is already set."))
        #expect(!already.aboutTheDigits)
        #expect(already.text == "A PIN is already set.")
        let timeout = PinWording.failure(.timedOut(command: "keryx pin set"))
        #expect(!timeout.aboutTheDigits)
        #expect(timeout.text == KeryxError.timedOut(command: "keryx pin set").message)
    }

    @Test func savedSaysWhetherARestartIsNeeded() {
        #expect(PinWording.saved(replaced: false, restartNeeded: false) == "The PIN is set.")
        #expect(PinWording.saved(replaced: true, restartNeeded: true)
            == "The PIN is changed. Keryx is running with the PIN it started with until it restarts.")
    }
}
