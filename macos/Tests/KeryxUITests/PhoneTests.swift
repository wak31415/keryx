import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// The Phone section's words for `phone numbers|webhooks|tunnel --json`.
@Suite struct PhoneWordingTests {
    @Test func theAccountIsItsNameOrWhyNot() {
        #expect(PhoneWording.account(PreviewData.phoneNumbers, sidSet: true, tokenSet: true)
            == PhoneWording.Line(tone: .ok, text: "Ada's account"))
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
        #expect(said.hasPrefix("The new account SID was saved, but the auth token wasn't: Could not write secrets.toml."))
        #expect(said.contains("Save again"))
        #expect(PhoneWording.saveFailure(error) == "Could not write secrets.toml.")
    }

        @Test func thePairCheckNamesTheAccount() {
        #expect(PhoneWording.accountCheck(.works(detail: "Ada's account"))?.text == "Twilio accepted them: Ada's account.")
        #expect(PhoneWording.accountCheck(.works(detail: nil))?.text == "Twilio accepted them.")
        #expect(PhoneWording.accountCheck(.unchecked) == nil)
    }

    @Test func thePickerOffersTheAccountsNumbersAndTheSavedOne() {
        let choices = PhoneWording.choices(PreviewData.phoneNumbers.numbers, saved: "+15550001111")
        #expect(choices.map(\.label) == ["Office · +15550001111", "Keryx · +15550002222"])
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
        #expect(PhoneWording.tunnel(PreviewData.phoneTunnel).text == "cloudflared")
        #expect(PhoneWording.tunnel(PreviewData.phoneTunnelUnset).hint?.contains("brew install cloudflared") == true)
        #expect(PhoneWording.reachable(PreviewData.phoneTunnel).text == "Keryx answers at keryx.example.com")
        #expect(PhoneWording.reachable(PreviewData.phoneTunnelUnset).text == "No public hostname yet")
        var bad = PreviewData.phoneTunnel
        bad.reachable = false
        bad.error = "PUBLIC_HOST must be a bare hostname"
        #expect(PhoneWording.reachable(bad).hint == "PUBLIC_HOST must be a bare hostname.")
        #expect(PhoneWording.tunnelService(PreviewData.phoneTunnelUnset).text == "Not installed")
    }
}

/// The Overview's service rows.
@Suite struct ServiceWordingTests {
    @Test func quittingAsksWhileTheServiceIsChanging() {
        let alone = Quitting.question(downloading: false, settingUpVoice: false, assistant: "Lyra",
                                      changingService: .installing)
        #expect(alone?.message == "Keryx's service is being installed. Quit anyway?")
        #expect(alone?.detail == "It stops, and may be left part done. Install… in the Overview runs it again.")
        // With something else under way, one question names both.
        let both = Quitting.question(downloading: true, settingUpVoice: false, assistant: "Lyra",
                                     changingService: .uninstalling)
        #expect(both?.message == "A model is downloading and Keryx's service is being uninstalled. Quit anyway?")
        #expect(both?.detail == "Both stop. The download picks up where it left off, and the service's uninstall "
            + "may be left part done (Uninstall… in the Overview runs it again).")
        #expect(Quitting.question(downloading: false, settingUpVoice: false, assistant: "Lyra",
                                  changingService: nil) == nil)
    }

    @Test func quittingNamesTheServiceAndASignInTogether() {
        let both = Quitting.question(downloading: false, settingUpVoice: false, signingIn: "Claude",
                                     signInDetail: SignInWording.quitDetail(.claude), assistant: "Lyra",
                                     changingService: .installing)
        #expect(both?.message
            == "A sign-in to Claude is still going and Keryx's service is being installed. Quit anyway?")
        #expect(both?.detail.hasPrefix("Both stop. The sign-in to Claude can be started again later, ") == true)
        #expect(both?.detail.contains("the service's install may be left part done") == true)
    }

    @Test func uninstallingSaysWhatStopsAndWhatIsKept() {
        let message = ServiceWording.Change.uninstall.message(assistant: "Lyra", liveCalls: 0)
        #expect(message.contains("Lyra won't answer the phone"))
        #expect(message.contains("settings and what Keryx remembers are kept"))
        #expect(ServiceWording.Change.uninstall.message(assistant: "Lyra", liveCalls: 1).hasSuffix("The call in progress ends."))
        #expect(ServiceWording.Change.uninstall.message(assistant: "Lyra", liveCalls: 2).hasSuffix("The 2 calls in progress end."))
        #expect(ServiceWording.Change.install.question == "Install Keryx as a Service?")
        #expect(ServiceWording.Change.uninstall.button == "Uninstall")
    }

    @Test func theTunnelUnitIsAWord() {
        #expect(ServiceWording.tunnel(PreviewData.service.units.tunnel).text == "Running")
        #expect(ServiceWording.tunnel(PreviewData.service.units.voice).text == "Not installed")
        #expect(ServiceWording.tunnel(PreviewData.service.units.llm).text == "Stopped")
    }
}
