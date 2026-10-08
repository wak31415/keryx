import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// The Phone section's words for `phone numbers|webhooks|tunnel --json`.
@Suite struct PhoneWordingTests {
    @Test func theAccountIsItsNameOrWhyNot() {
        #expect(PhoneWording.account(PreviewData.phoneNumbers, sidSet: true, tokenSet: true)
            == PhoneWording.Line(tone: .ok, text: "Connected", hint: "Ada's account"), "the name in the caption")
        #expect(PhoneWording.account(PreviewData.phoneNumbersUnset, sidSet: false, tokenSet: false).text == "Not connected")
        var refused = PreviewData.phoneNumbersUnset
        refused.error = "Twilio didn't accept the account SID and auth token."
        let line = PhoneWording.account(refused, sidSet: true, tokenSet: true)
        #expect(line.tone == .error)
        #expect(line.hint == "Twilio didn't accept the account SID and auth token.")
    }

    @Test func anAccountSIDIsCheckedBeforeTwilioIsAsked() {
        #expect(PhoneWording.sidProblem("") == nil)
        #expect(PhoneWording.sidProblem("AC0123456789abcdef0123456789abcdef") == nil)
        #expect(PhoneWording.sidProblem(" AC0123456789ABCDEF0123456789abcdef ") == nil)
        #expect(PhoneWording.sidProblem("SK0123456789abcdef0123456789abcdef") != nil)
        #expect(PhoneWording.sidProblem("AC0123") != nil)
        #expect(PhoneWording.sidProblem("AC0123456789abcdef0123456789abcdeg") != nil)
    }

    @Test func aHalfSavedPairIsSaidAsOne() {
        let error = KeryxError.failed(command: "keryx config set", exitCode: 1, message: "could not write secrets.toml")
        let said = PhoneWording.tokenNotSaved(error)
        #expect(said.message.hasPrefix("The new account SID was saved, but the auth token wasn't."))
        #expect(said.message.contains("Save again"))
        #expect(said.detail == "could not write secrets.toml")
        #expect(PhoneWording.saveFailure(error)
            == StreamFailure("The Twilio account wasn't saved.", detail: "could not write secrets.toml"))
    }

        @Test func thePairCheckNamesTheAccount() {
        #expect(PhoneWording.accountCheck(.works(detail: "Ada's account"))?.text == "Twilio accepted them: Ada's account.")
        #expect(PhoneWording.accountCheck(.works(detail: nil))?.text == "Twilio accepted them.")
        #expect(PhoneWording.accountCheck(.unchecked) == nil)
    }

    @Test func thePickerOffersTheAccountsNumbersAndTheSavedOne() {
        let choices = PhoneWording.choices(PreviewData.phoneNumbers.numbers, saved: "+15550001111")
        #expect(choices.map(\.label) == ["Office · +1 555 000 1111", "Keryx · +1 555 000 2222"])
        #expect(choices.map(\.pointsHere) == [false, true])
        let foreign = PhoneWording.choices(PreviewData.phoneNumbers.numbers, saved: "+15550009999")
        #expect(foreign.first?.number == "+15550009999")
        #expect(foreign.first?.label == "+15550009999 (not in this account)")
        #expect(PhoneWording.choices([], saved: nil).isEmpty)
    }

    @Test func theNumbersCaptionSaysWhatToDoFirst() {
        let titles = ["TWILIO_ACCOUNT_SID": "Twilio account SID", "TWILIO_AUTH_TOKEN": "Twilio auth token"]
        #expect(PhoneWording.numberCaption(Loadable(value: PreviewData.phoneNumbersUnset), connected: false, titles: titles)
            == "Connect your Twilio account first, then choose its number.")
        #expect(PhoneWording.numberCaption(Loadable(value: PreviewData.phoneNumbersUnset), connected: true, titles: titles)
            == "Its numbers are listed here once Twilio answers.", "the account row says why")
        #expect(PhoneWording.numberCaption(Loadable(value: PreviewData.phoneNumbers), connected: true, titles: titles)
            == "The number Keryx answers and calls from.")
    }

    @Test func theWebhooksSayWhereCallsGo() {
        #expect(PhoneWording.webhooks(PreviewData.phoneWebhooksMatching).tone == .ok)
        #expect(PhoneWording.webhooks(PreviewData.phoneWebhooks).text == "+15550001111's calls go elsewhere")
        #expect(PhoneWording.webhooks(PreviewData.phoneWebhooksUnset, titles: ["TWILIO_NUMBER": "Twilio number"]).hint
            == "There's no “Twilio number” saved.")
        var unasked = PreviewData.phoneWebhooks
        unasked.current = nil
        #expect(PhoneWording.webhooks(unasked).text.hasPrefix("Couldn't ask Twilio"))
    }

    @Test func pointHereActsOnlyOnTheSheetsOwnSuccessfulRead() {
        let report = PreviewData.phoneWebhooks
        #expect(PhoneWording.canPointHere(fresh: report, asking: false, readFailed: false, applying: false))
        // Before this sheet's read has answered: what the Phone section showed is not enough.
        #expect(!PhoneWording.canPointHere(fresh: nil, asking: true, readFailed: false, applying: false))
        #expect(!PhoneWording.canPointHere(fresh: report, asking: true, readFailed: false, applying: false))
        // The read failed: whatever is shown may be stale.
        #expect(!PhoneWording.canPointHere(fresh: nil, asking: false, readFailed: true, applying: false))
        #expect(!PhoneWording.canPointHere(fresh: report, asking: false, readFailed: true, applying: false))
        #expect(!PhoneWording.canPointHere(fresh: report, asking: false, readFailed: false, applying: true))
        #expect(!PhoneWording.canPointHere(fresh: PreviewData.phoneWebhooksMatching, asking: false,
                                           readFailed: false, applying: false))
    }

    @Test func pointHereIsAClickNeverReturn() throws {
        // The one write to Twilio is not the sheet's default action.
        let source = try String(contentsOf: URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KeryxUI/Phone/PointHereSheet.swift"), encoding: .utf8)
        let button = try #require(source.range(of: #"Button("Point Here""#))
        let rest = source[button.upperBound...]
        let end = rest.range(of: "\n            }")?.lowerBound ?? rest.endIndex
        #expect(!rest[..<end].contains("keyboardShortcut"))
    }

    @Test func pointHereIsOfferedOnlyWhenThereIsSomethingToPoint() {
        #expect(PhoneWording.canApply(PreviewData.phoneWebhooks))
        #expect(!PhoneWording.canApply(PreviewData.phoneWebhooksMatching), "already here")
        #expect(!PhoneWording.canApply(PreviewData.phoneWebhooksUnset), "no number, no addresses")
        var unasked = PreviewData.phoneWebhooks
        unasked.current = nil
        #expect(!PhoneWording.canApply(unasked), "the question shows where it points now: Twilio must have said")
        var failed = PreviewData.phoneWebhooks
        failed.error = "Twilio didn't answer"
        #expect(!PhoneWording.canApply(failed))
    }

    @Test func theQuestionShowsBothAddressesNowAndNew() {
        let hooks = PhoneWording.hooks(PreviewData.phoneWebhooks)
        #expect(hooks.map(\.title) == ["Incoming calls", "Call status"])
        #expect(hooks[0].now == "https://demo.twilio.com/welcome/voice/")
        #expect(hooks[0].expected == "https://keryx.example.com/twilio/voice")
        #expect(hooks[1].now == "https://old.example.com/status")
        #expect(hooks[1].expected == "https://keryx.example.com/twilio/status")
        #expect(!hooks[0].matches)
        #expect(PhoneWording.hooks(PreviewData.phoneWebhooksMatching).allSatisfy { $0.matches })
        #expect(PhoneWording.Hook(title: "x", now: "https://a/b/", expected: "https://a/b").matches,
                "a trailing slash aside, as Keryx compares them")
        #expect(PhoneWording.applyTitle(PreviewData.phoneWebhooks) == "Point +15550001111 at This Mac?")
    }

    @Test func theTunnelSaysWhatIsMissing() {
        #expect(PhoneWording.tunnel(PreviewData.phoneTunnel) == PhoneWording.Line(tone: .ok, text: "Installed", hint: "cloudflared"))
        #expect(PhoneWording.tunnel(PreviewData.phoneTunnelUnset).hint.map(AppFacing.isFit) == true, "no command")
        #expect(PhoneWording.reachable(PreviewData.phoneTunnel).text == "Keryx answers at keryx.example.com")
        #expect(PhoneWording.reachable(PreviewData.phoneTunnelUnset).text == "No public hostname yet")
        var bad = PreviewData.phoneTunnel
        bad.reachable = false
        bad.error = "PUBLIC_HOST must be a bare hostname"
        #expect(PhoneWording.reachable(bad).hint == "PUBLIC_HOST must be a bare hostname.")
        #expect(PhoneWording.tunnelService(PreviewData.phoneTunnelUnset).text == "Not yet")
    }
}

/// The Overview's "Answering calls" row, and what quitting asks.
@Suite struct ServiceWordingTests {
    static var notInstalled: StatusReport {
        var status = PreviewData.status
        status.service.installed = false
        status.service.active = false
        status.health.reachable = false
        return status
    }

    @Test func aRunningKeryxAnswersAndNamesItsUnitOnlyInTheTooltip() {
        let line = ServiceWording.answering(PreviewData.status, step: .running, change: nil, failure: nil)
        #expect(line.text == "Yes" && line.tone == .ok && !line.offersStart)
        #expect(line.help == "Running as launchd dev.keryx.agent")
        #expect(line.hint == nil)
        for text in [line.text, line.hint ?? ""] {
            #expect(!text.lowercased().contains("service") && !text.contains("launchd"))
        }
    }

    @Test func startIsOfferedOnlyWhenKeryxShouldRunAndDoesNot() {
        let should = ServiceWording.answering(Self.notInstalled, step: .start, change: nil, failure: nil)
        #expect(should.text == "No" && should.offersStart)
        let waiting = ServiceWording.answering(Self.notInstalled, step: .waitingForSetup, change: nil, failure: nil)
        #expect(waiting.text == "Not yet" && !waiting.offersStart)
        #expect(waiting.hint == "Keryx starts once setup is finished")
        let unknown = ServiceWording.answering(Self.notInstalled, step: .unknown, change: nil, failure: nil)
        #expect(!unknown.offersStart)
    }

    @Test func aFailedStartSaysWhyAndOffersStartAgain() {
        let failure = KeryxError.failed(command: "keryx service install", exitCode: 1,
                                        message: "PUBLIC_HOST is not set")
        let line = ServiceWording.answering(Self.notInstalled, step: .waitingForSetup, change: nil, failure: failure)
        #expect(line.tone == .error && line.offersStart)
        #expect(line.hint?.hasPrefix("Keryx couldn't start") == true)
    }

    @Test func aStartUnderWayIsAWordAndASpinner() {
        let line = ServiceWording.answering(Self.notInstalled, step: .start, change: .installing, failure: nil)
        #expect(line.text == "Starting…" && line.busy && !line.offersStart)
        #expect(ServiceWording.answering(PreviewData.status, step: .running, change: .uninstalling,
                                         failure: nil).text == "Stopping…")
    }

    @Test func aKeryxStartedByHandAnswersWithoutTheUnit() {
        var byHand = Self.notInstalled
        byHand.health.reachable = true
        let line = ServiceWording.answering(byHand, step: .running, change: nil, failure: nil)
        #expect(line.text == "Yes" && !line.offersStart)
        #expect(line.hint == "Started outside this app")
    }

    /// A relaunch into a Keryx the owner stopped (`keryx service stop`, Task N) says so
    /// quietly, never as an error, and only until it starts again.
    @Test func stoppedByTheOwnerIsSaidQuietlyUntilStarted() {
        var stopped = Self.notInstalled
        stopped.service.installed = true
        stopped.restart.state = "stopped"
        let waiting = ServiceWording.answering(stopped, step: .start, change: nil, failure: nil)
        #expect(waiting.hint == "Keryx was stopped by its owner")
        #expect(waiting.tone != .error, "quiet, never an error")
        var running = stopped
        running.service.active = true
        let answeringNow = ServiceWording.answering(running, step: .running, change: nil, failure: nil)
        #expect(answeringNow.hint == nil, "gone once it answers again")
    }

    @Test func quittingAsksWhileKeryxStarts() {
        let alone = Quitting.question(downloading: false, settingUpVoice: false, assistant: "Lyra",
                                      changingService: .installing)
        #expect(alone?.message == "Keryx is starting. Quit anyway?")
        #expect(alone?.detail == "It stops part way.")
        #expect(alone?.suppressible == false)
        // With something else under way, one question names both.
        let both = Quitting.question(downloading: true, settingUpVoice: false, assistant: "Lyra",
                                     changingService: .installing)
        #expect(both?.message == "A model is downloading and Keryx is starting. Quit anyway?")
        #expect(both?.detail == "Both stop. The download picks up where it left off, and Keryx stops starting.")
        #expect(Quitting.question(downloading: false, settingUpVoice: false, assistant: "Lyra",
                                  changingService: nil) == nil)
    }

    @Test func quittingSaysItStopsAnsweringCalls() {
        let plain = Quitting.question(downloading: false, settingUpVoice: false, assistant: "Lyra",
                                      stopsAnswering: true, confirm: true)
        #expect(plain == Quitting.Question(
            message: "Quit Keryx?",
            detail: "Lyra stops answering calls until you open Keryx again. To keep answering, close the window instead.",
            suppressible: true))
        // Once the owner has said not to ask, a plain quit just quits.
        #expect(Quitting.question(downloading: false, settingUpVoice: false, assistant: "Lyra",
                                  stopsAnswering: true, confirm: false) == nil)
        // Without a unit to stop, nothing to say.
        #expect(Quitting.question(downloading: false, settingUpVoice: false, assistant: "Lyra",
                                  stopsAnswering: false, confirm: true) == nil)
        // Anything else under way says it too.
        let download = Quitting.question(downloading: true, settingUpVoice: false, assistant: "Lyra",
                                         stopsAnswering: true, confirm: false)
        #expect(download?.detail == "It will pick up where it left off next time. "
            + "Lyra stops answering calls until you open Keryx again.")
        #expect(download?.suppressible == false)
    }

    @Test func aCallInProgressAlwaysAsks() {
        let one = Quitting.question(downloading: false, settingUpVoice: false, assistant: "Lyra",
                                    stopsAnswering: true, liveCalls: 1, confirm: false)
        #expect(one == Quitting.Question(
            message: "1 call is in progress. Quit anyway?",
            detail: "The call in progress ends, and Lyra stops answering calls until you open Keryx again."))
        let two = Quitting.question(downloading: false, settingUpVoice: false, assistant: nil,
                                    stopsAnswering: true, liveCalls: 2, confirm: false)
        #expect(two?.message == "2 calls are in progress. Quit anyway?")
        #expect(two?.detail == "The 2 calls in progress end, and the assistant stops answering calls "
            + "until you open Keryx again.")
        #expect(two?.suppressible == false)
    }

    @Test func quittingNamesAWriteStillGoing() {
        let alone = Quitting.question(downloading: false, settingUpVoice: false, assistant: "Lyra",
                                      writing: [.webhooks])
        #expect(alone?.message == "The phone number's webhooks are being changed. Quit anyway?")
        #expect(alone?.detail == "It stops part way, and may leave the change half made.")
        let both = Quitting.question(downloading: true, settingUpVoice: false, assistant: "Lyra",
                                     writing: [.setting, .pin])
        #expect(both?.message
            == "A model is downloading and a setting is being saved and the phone PIN is being saved. Quit anyway?")
        #expect(both?.detail.hasSuffix("and a change cut short may be left half made.") == true)
    }

    @Test func quittingNamesKeryxStartingAndASignInTogether() {
        let both = Quitting.question(downloading: false, settingUpVoice: false, signingIn: "Claude",
                                     signInDetail: SignInWording.quitDetail(.claude), assistant: "Lyra",
                                     changingService: .installing)
        #expect(both?.message
            == "A sign-in to Claude is still going and Keryx is starting. Quit anyway?")
        #expect(both?.detail.hasPrefix("Both stop. The sign-in to Claude can be started again later, ") == true)
        #expect(both?.detail.contains("Keryx stops starting") == true)
    }

    @Test func theTunnelUnitIsAWord() {
        #expect(ServiceWording.tunnel(PreviewData.service.units.tunnel).text == "Running")
        #expect(ServiceWording.tunnel(PreviewData.service.units.voice).text == "Not installed")
        #expect(ServiceWording.tunnel(PreviewData.service.units.llm).text == "Not running")
    }
}
