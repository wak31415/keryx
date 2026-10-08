import AppKit
import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// The app's life beside its windows and Keryx's: in the Dock for as long as it runs,
/// closing a window leaves Keryx answering, and Quit stops it — said where the owner looks.
@Suite struct AppLifecycleTests {
    /// The bundle's Info.plist, as build-app.sh copies it.
    static func infoPlist() throws -> [String: Any] {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Resources/Info.plist")
        let data = try Data(contentsOf: url)
        return try #require(try PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any])
    }

    @Test func theAppIsInTheDockWhetherOrNotAWindowIsOpen() throws {
        // Not an agent app: LSUIElement would start it with no Dock icon.
        let plist = try Self.infoPlist()
        #expect((plist["LSUIElement"] as? Bool ?? false) == false)
        #expect(AppLifecycle.activationPolicy == .regular)
    }

    @Test func closingTheLastWindowNeverQuits() {
        #expect(AppLifecycle.quitsWhenLastWindowCloses == false)
    }

    static func quitEvent(reason: OSType?) -> NSAppleEventDescriptor {
        let event = NSAppleEventDescriptor(eventClass: AEEventClass(kCoreEventClass),
                                           eventID: AEEventID(kAEQuitApplication), targetDescriptor: nil,
                                           returnID: AEReturnID(kAutoGenerateReturnID),
                                           transactionID: AETransactionID(kAnyTransactionID))
        if let reason {
            event.setAttribute(NSAppleEventDescriptor(enumCode: reason), forKeyword: AEKeyword(kAEQuitReason))
        }
        return event
    }

    @Test func aLogOutRestartOrShutDownAsksNothing() {
        for reason in [kAELogOut, kAEReallyLogOut, kAEShowRestartDialog, kAERestart, kAEShowShutdownDialog,
                       kAEShutDown] {
            #expect(AppLifecycle.isSessionEnding(Self.quitEvent(reason: OSType(reason))))
        }
    }

    @Test func anOrdinaryQuitIsNotTheSessionEnding() {
        #expect(!AppLifecycle.isSessionEnding(Self.quitEvent(reason: nil)), "⌘Q, or Quit in the menu")
        #expect(!AppLifecycle.isSessionEnding(nil))
        let open = NSAppleEventDescriptor(eventClass: AEEventClass(kCoreEventClass),
                                          eventID: AEEventID(kAEOpenApplication), targetDescriptor: nil,
                                          returnID: AEReturnID(kAutoGenerateReturnID),
                                          transactionID: AETransactionID(kAnyTransactionID))
        #expect(!AppLifecycle.isSessionEnding(open))
    }

    // MARK: - Wording

    @Test func theMenuSaysQuittingStopsAnswering() {
        #expect(MenuContent.quitNote("Lyra") == "Lyra stops answering calls when Keryx quits")
        #expect(MenuContent.quitNote(nil) == "The assistant stops answering calls when Keryx quits")
    }

    @Test func openAtLoginAsksForARegistrationAgainOnlyWhenSignedAdHoc() {
        let signed = AppSettingsView.loginFooter(survivesRebuilds: true)
        #expect(signed.hasPrefix("Keryx must be in Applications."))
        #expect(!signed.contains("off and on again"))
        let adHoc = AppSettingsView.loginFooter(survivesRebuilds: false)
        #expect(adHoc.contains("turn this off and on again"))
    }

    @Test func theServiceChecksAreSaidInTheAppsWords() {
        #expect(CheckWording.title("Service manager") == "Runs in the background")
        #expect(CheckWording.title("Service running") == "Running")
        // Task N's renamed doctor check titles say the same thing in the same words.
        #expect(CheckWording.title("Background process") == "Runs in the background")
        #expect(CheckWording.title("Keryx running") == "Running")
        #expect(CheckWording.title("OpenAI API key") == "OpenAI API key")
        let said = CheckWording.detail("dev.keryx.agent is not installed — Keryx cannot restart itself")
        #expect(said.text == "Keryx isn't answering calls yet — Keryx cannot restart itself")
        #expect(!said.text.lowercased().contains("service"))
    }

    @Test func theFixForAMissingUnitIsStartKeryx() {
        let action = FixAction("service:install")?.action(SetupContext())
        #expect(action == SetupAction(title: "Start Keryx", effect: .startKeryx))
    }

    @Test func theDoctorsServiceSectionIsAnsweringCalls() {
        #expect(WizardWording.answeringCalls == "Answering Calls")
        #expect(WizardWording.checkRow(.service, plan: nil) == "Answering Calls", "the wizard says it the same way")
    }
}
