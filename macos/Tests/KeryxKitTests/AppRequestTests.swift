import Foundation
import Testing
@testable import KeryxKit

/// What the Setup Assistant asks of the main window: the section each request shows, and
/// how long a request stays pending.
@MainActor
@Suite struct AppRequestTests {
    func makeModel() -> AppModel {
        AppModel(locator: StubLocator(), makeClient: { _ in StubClient() },
                 preferences: Preferences(defaults: freshDefaults()), timing: AppModelTests.noPoll)
    }

    @Test func eachRequestShowsTheSectionThatAnswersIt() {
        #expect(AppRequest.show(.phone).destination == .phone)
        #expect(AppRequest.configure(tab: "agents").destination == .configuration)
        #expect(AppRequest.setPin.destination == .configuration)
        #expect(AppRequest.signIn(.claude).destination == .configuration)
        #expect(AppRequest.signIn(.codex).destination == .configuration)
        #expect(AppRequest.signIn(.gmail).destination == .integrations)
        #expect(AppRequest.signIn(.googleWorkspace).destination == .integrations)
        #expect(AppRequest.connect(integration: "google").destination == .integrations)
        #expect(AppRequest.setUpVoiceHere.destination == .voice)
    }

    @Test func aRequestSelectsItsSectionAndWaitsToBeAnswered() {
        let model = makeModel()
        model.request(.setPin)
        #expect(model.selection == .configuration)
        #expect(model.requested == .setPin)
        model.didHandle(.setPin)
        #expect(model.requested == nil)
    }

    @Test func answeringAnOlderRequestLeavesANewerOne() {
        let model = makeModel()
        model.request(.signIn(.gmail))
        model.request(.connect(integration: "google"))
        model.didHandle(.signIn(.gmail))
        #expect(model.requested == .connect(integration: "google"))
        #expect(model.selection == .integrations)
    }

    @Test func choosingAnotherSectionDropsAnUnansweredRequest() {
        let model = makeModel()
        model.request(.setUpVoiceHere)
        model.selection = .voice
        #expect(model.requested == .setUpVoiceHere, "the same section keeps it")
        model.selection = .sessions
        #expect(model.requested == nil)
        // A request for another section replaces the one before, whatever it was.
        model.request(.setPin)
        model.request(.show(.phone))
        #expect(model.requested == .show(.phone))
        #expect(model.selection == .phone)
    }
}
