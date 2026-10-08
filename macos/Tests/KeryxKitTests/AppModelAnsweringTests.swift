import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview

/// Keeping Keryx answering calls while the app is open: started once setup has reached the
/// service step, never before a migration or while a step it waits for is left, never beside
/// a Keryx started by hand, and stopped when the app quits.
@MainActor
@Suite struct AppModelAnsweringTests {
    // MARK: - Documents

    /// The status of a Mac where Keryx's unit is not installed and nothing answers.
    static var notInstalled: StatusReport {
        var status = PreviewData.status
        status.service.installed = false
        status.service.active = false
        status.health.reachable = false
        status.health.liveSessions = nil
        return status
    }

    /// Installed, but not running.
    static var stopped: StatusReport {
        var status = notInstalled
        status.service.installed = true
        return status
    }

    /// The ready plan with the service step in `state`, and `fix` as its fix.
    static func plan(service state: String, fix: String? = nil) -> SetupPlan {
        var plan = PreviewData.setupPlanReady
        let index = plan.steps.firstIndex { $0.id == "service" }!
        plan.steps[index].state = state
        plan.steps[index].fix = fix
        plan.ready = state == "done"
        return plan
    }

    /// The plan the CLI prints for a unit installed but not running: the step fails on its
    /// "Background process" check (Task N), whose fix is `service:start`.
    static var stoppedPlan: SetupPlan { plan(service: "failed", fix: "service:start") }

    /// `plan` with a migration still to run in front of every step.
    static func migrating(_ plan: SetupPlan) -> SetupPlan {
        var plan = plan
        var migrate = plan.steps[0]
        migrate.id = "migrate"
        migrate.title = "Migration"
        migrate.state = "needed"
        plan.steps.insert(migrate, at: 0)
        plan.ready = false
        return plan
    }

    /// A service report with Keryx's unit `installed` or not.
    static func units(installed: Bool) -> ServiceReport {
        var report = PreviewData.service
        report.units.agent.installed = installed
        report.units.agent.active = installed
        return report
    }

    func makeModel(client: StubClient, keepsAnswering: Bool = true) -> AppModel {
        let model = AppModel(locator: StubLocator(), makeClient: { _ in client },
                             preferences: Preferences(defaults: freshDefaults()),
                             timing: AppModelTests.noPoll)
        model.keepsAnswering = keepsAnswering
        return model
    }

    func installs(_ client: StubClient) -> Int {
        client.calls.filter { $0 == "service install" }.count
    }

    /// `keryx service start --all`: a unit that is already there, started again along with
    /// every installed model server (C1, Task N: `start --all` acts only on what is
    /// installed, so this is as safe at launch as a plain `start`).
    func starts(_ client: StubClient) -> Int {
        client.calls.filter { $0 == "service start --all" }.count
    }

    // MARK: - The decision

    @Test func setupThatHasReachedTheServiceStepStartsKeryx() {
        #expect(AppModel.answeringStep(plan: Self.plan(service: "needed"), status: Self.notInstalled) == .start)
        #expect(AppModel.answeringStep(plan: Self.stoppedPlan, status: Self.stopped) == .start,
                "installed but stopped is started too, as the CLI says it: failed, fixed by a start")
        #expect(AppModel.answeringStep(plan: Self.plan(service: "done"), status: Self.stopped) == .start,
                "a plan read before it stopped")
        #expect(AppModel.answeringStep(plan: Self.plan(service: "failed", fix: "service:install"),
                                       status: Self.notInstalled) == .start,
                "a legacy failed service:install fix (not yet installed) still starts Keryx")
    }

    @Test func nothingStartsBeforeSetupHasReachedIt() {
        for state in ["blocked", "failed"] {
            #expect(AppModel.answeringStep(plan: Self.plan(service: state), status: Self.notInstalled) == .waitingForSetup,
                    "service \(state)")
        }
        #expect(AppModel.answeringStep(plan: Self.plan(service: "failed", fix: "secret:OPENAI_API_KEY"),
                                       status: Self.stopped) == .waitingForSetup, "failed for something a start can't mend")
        #expect(AppModel.answeringStep(plan: PreviewData.setupPlan, status: Self.notInstalled) == .waitingForSetup,
                "a fresh Mac: the phone, the voice and the agents are left")
        #expect(AppModel.answeringStep(plan: Self.migrating(Self.plan(service: "needed")), status: Self.notInstalled)
            == .waitingForSetup, "never before the migration, whatever the service step says")
    }

    @Test func nothingStartsWithoutAStatusThatCanBeTrusted() {
        var migration = Self.notInstalled
        migration.storage.migrationRequired = true
        var unreadable = Self.notInstalled
        unreadable.storage.ok = false
        var noManager = Self.notInstalled
        noManager.service.manager = nil
        var unknown = Self.notInstalled
        unknown.service.ok = false
        let plan = Self.plan(service: "needed")
        for status in [migration, unreadable, noManager, unknown] {
            #expect(AppModel.answeringStep(plan: plan, status: status) == .unknown)
        }
        #expect(AppModel.answeringStep(plan: plan, status: nil) == .unknown)
        #expect(AppModel.answeringStep(plan: nil, status: Self.notInstalled) == .unknown)
    }

    @Test func aRunningKeryxIsLeftAlone() {
        #expect(AppModel.answeringStep(plan: Self.plan(service: "done"), status: PreviewData.status) == .running)
        var byHand = Self.notInstalled
        byHand.health.reachable = true
        #expect(AppModel.answeringStep(plan: Self.plan(service: "needed"), status: byHand) == .running,
                "a keryx serve started by hand answers already; a second would fight it for the port")
    }

    // MARK: - At launch

    @Test func launchStartsKeryxOnceSetupAllowsIt() async throws {
        let client = StubClient {
            $0.status = .success(Self.notInstalled)
            $0.setupPlan = .success(Self.plan(service: "needed"))
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.ensureAnswering()
        #expect(installs(client) == 1)
        let calls = client.calls
        let plan = calls.firstIndex(of: "setup plan") ?? .max
        let install = try #require(calls.firstIndex(of: "service install"))
        #expect(plan < install, "the plan is read before anything starts")
        #expect(model.startFailure == nil)
    }

    @Test func launchStartsAStoppedUnitAgain() async {
        // Installed but stopped (`service:start`, Task N): started again, never installed.
        let client = StubClient {
            $0.status = .success(Self.stopped)
            $0.setupPlan = .success(Self.stoppedPlan)
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.ensureAnswering()
        #expect(starts(client) == 1)
        #expect(installs(client) == 0)
    }

    @Test func nothingStartsUnlessTheModelKeepsKeryxAnswering() async {
        let client = StubClient {
            $0.status = .success(Self.notInstalled)
            $0.setupPlan = .success(Self.plan(service: "needed"))
        }
        let model = makeModel(client: client, keepsAnswering: false)
        await model.start()
        defer { model.stop() }
        await model.ensureAnswering()
        await model.refresh(.setupPlan)
        #expect(installs(client) == 0)
    }

    @Test func nothingStartsBeforeTheMigration() async {
        var migration = Self.notInstalled
        migration.storage.migrationRequired = true
        let client = StubClient {
            $0.status = .success(migration)
            $0.setupPlan = .success(Self.migrating(Self.plan(service: "needed")))
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.ensureAnswering()
        #expect(installs(client) == 0)
    }

    @Test func nothingStartsWhileARequiredStepIsLeft() async {
        let client = StubClient {
            $0.status = .success(Self.notInstalled)
            $0.setupPlan = .success(PreviewData.setupPlanPartway)
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.ensureAnswering()
        #expect(installs(client) == 0)
        #expect(model.answeringStep == .waitingForSetup)
    }

    @Test func aRunningKeryxIsNotStartedAgain() async {
        let client = StubClient { $0.setupPlan = .success(PreviewData.setupPlanReady) }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.ensureAnswering()
        #expect(installs(client) == 0)
        #expect(model.answeringStep == .running)
    }

    @Test func finishingSetupStartsKeryxWhenThePlanIsReadAgain() async {
        let client = StubClient {
            $0.status = .success(Self.notInstalled)
            $0.setupPlan = .success(Self.plan(service: "blocked"))
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.ensureAnswering()
        #expect(installs(client) == 0)
        // The phone set up in the Setup Assistant: it reads the plan again.
        client.update { $0.setupPlan = .success(Self.plan(service: "needed")) }
        await model.refresh(.setupPlan)
        #expect(await eventually { installs(client) == 1 })
        await model.ensureAnswering()
        #expect(installs(client) == 1)
    }

    @Test func aFailedStartIsSaidAndNotTriedAgainByItself() async {
        let failure = KeryxError.failed(command: "keryx service install", exitCode: 1,
                                        message: "PUBLIC_HOST is not set")
        let client = StubClient {
            $0.status = .success(Self.notInstalled)
            $0.setupPlan = .success(Self.plan(service: "needed"))
            // Named explicitly: the failed install's own reread (I4) now loads `.service`
            // too, and it must go on saying "not installed" for the retry below to take
            // the install path again, not `service start`.
            $0.service = .success(Self.units(installed: false))
            $0.serviceResult = .failure(failure)
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.ensureAnswering()
        #expect(model.startFailure == failure)
        await model.refresh(.setupPlan)
        await model.ensureAnswering()
        #expect(installs(client) == 1, "once a launch")
        // Start in the Overview tries again, and one that works clears the failure.
        client.update { $0.serviceResult = .success(ServiceResult(ok: true, message: "installed")) }
        #expect(await model.startAnswering() == .success(ServiceResult(ok: true, message: "installed")))
        #expect(model.startFailure == nil)
        #expect(installs(client) == 2)
    }

    @Test func aStartByHandIsNotFollowedByAnAutomaticOne() async {
        let client = StubClient {
            $0.status = .success(Self.notInstalled)
            $0.setupPlan = .success(Self.plan(service: "blocked"))
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.ensureAnswering()
        client.update { $0.setupPlan = .success(Self.plan(service: "needed")) }
        // Start, while the status still says it is not running: the plan it reads again after
        // must not start it a second time.
        _ = await model.startAnswering()
        await model.ensureAnswering()
        #expect(installs(client) == 1)
    }

    @Test func choosingAnotherKeryxMayStartItOnceMore() async {
        let failure = KeryxError.failed(command: "keryx service install", exitCode: 2, message: "no checkout")
        let client = StubClient {
            $0.status = .success(Self.notInstalled)
            $0.setupPlan = .success(Self.plan(service: "needed"))
            $0.serviceResult = .failure(failure)
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.ensureAnswering()
        #expect(installs(client) == 1)
        client.update { $0.serviceResult = .success(ServiceResult(ok: true, message: "installed")) }
        await model.relocate()
        #expect(model.startFailure == nil)
        await model.ensureAnswering()
        #expect(installs(client) == 2)
    }

    // MARK: - Quitting

    @Test func quittingStopsAnInstalledKeryxAndReadsNothingAfter() async {
        let client = StubClient {
            $0.setupPlan = .success(PreviewData.setupPlanReady)
            $0.service = .success(Self.units(installed: true))
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.ensureAnswering()
        #expect(model.quitStopsAnswering)
        let mark = client.calls.count
        #expect(await model.stopAnswering() == nil)
        // I2: no `.service` read first to decide whether to stop — the stop always runs,
        // and the CLI is the one that says there was nothing to do.
        #expect(Array(client.calls.dropFirst(mark)) == ["service stop --all"])
    }

    /// I2: the agent's own unit not being installed (a Keryx started by hand, or This
    /// Mac's Set Up… having only started a model server) no longer gates the stop at
    /// all — the CLI itself says there was nothing to stop when there wasn't, and a
    /// model server or the voice can be running without the agent ever having been
    /// installed. Quitting still never *says* it stops answering calls, since the agent
    /// was never one to answer with.
    @Test func quittingStopsServersEvenWithTheAgentUnitNeverInstalled() async {
        var byHand = Self.notInstalled
        byHand.health.reachable = true
        let client = StubClient {
            $0.status = .success(byHand)
            $0.service = .success(Self.units(installed: false))
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        #expect(!model.quitStopsAnswering, "wording only: the agent was never installed")
        #expect(model.quitRunsServiceStop, "the stop itself still runs")
        await model.stopAnswering()
        #expect(client.calls.contains("service stop --all"))
    }

    @Test func quittingStopsNothingUnlessTheModelKeepsKeryxAnswering() async {
        let client = StubClient { $0.service = .success(Self.units(installed: true)) }
        let model = makeModel(client: client, keepsAnswering: false)
        await model.start()
        defer { model.stop() }
        #expect(!model.quitStopsAnswering)
        let mark = client.calls.count
        await model.stopAnswering()
        #expect(client.calls.count == mark, "it asks nothing")
    }

    @Test func quittingWithoutKeryxFoundDoesNothing() async {
        let client = StubClient()
        let model = AppModel(locator: StubLocator(.failure(LocateFailure(lookedAt: ["nowhere"]))),
                             makeClient: { _ in client }, preferences: Preferences(defaults: freshDefaults()),
                             timing: AppModelTests.noPoll)
        model.keepsAnswering = true
        await model.start()
        defer { model.stop() }
        #expect(!model.quitStopsAnswering)
        #expect(await model.stopAnswering() == nil)
        #expect(client.calls.isEmpty)
    }

    @Test func quittingDuringAStartWaitsForItThenStopsKeryx() async throws {
        let gate = OptionsGate()
        let client = StubClient {
            $0.status = .success(Self.notInstalled)
            $0.setupPlan = .success(Self.plan(service: "needed"))
            $0.serviceWait = { await gate.wait() }
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        #expect(await eventually { gate.held == 1 })
        #expect(model.serviceChange == .installing)
        #expect(model.quitStopsAnswering, "a unit being installed is stopped too")
        // The install finishes (the unit now installed); quitting then stops it.
        client.update { $0.service = .success(Self.units(installed: true)) }
        let stopping = Task { await model.stopAnswering() }
        gate.open()
        #expect(await stopping.value == nil)
        // `stopping.value` is awaited above, so every call either of them made is already
        // recorded (no fixed point read here: both calls are on the far side of an await).
        let calls = client.calls
        let install = try #require(calls.firstIndex(of: "service install"))
        let stop = try #require(calls.firstIndex(of: "service stop --all"))
        #expect(install < stop)
    }

    /// I1: the real CLI never throws on a failed stop — it prints `ok: false`, exit 1 —
    /// and the app is the one that turns that into a failure, with the reply's own
    /// `detail` carried under Show Details.
    @Test func aFailedStopIsReturned() async throws {
        let client = StubClient {
            $0.setupPlan = .success(PreviewData.setupPlanReady)
            $0.service = .success(Self.units(installed: true))
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.ensureAnswering()
        client.update {
            $0.serviceControlResult = .success(ServiceControlResult(
                ok: false, message: "Not everything could be stopped.",
                detail: "dev.keryx.tunnel: launchctl bootout: Input/output error"))
        }
        let error = try #require(await model.stopAnswering())
        #expect(error.details == "dev.keryx.tunnel: launchctl bootout: Input/output error")
    }

    /// I1: the same, for a start — the Overview's visible banner (`startFailure`), with
    /// Show Details carrying the reply's own `detail`, and Try Again (`offersStart`,
    /// `ServiceWordingTests`).
    @Test func aFailedStartWithTheRealReplyShapeSetsStartFailure() async throws {
        let client = StubClient {
            $0.status = .success(Self.stopped)
            $0.setupPlan = .success(Self.stoppedPlan)
            $0.serviceControlResult = .success(ServiceControlResult(
                ok: false, message: "Not everything could be started.", detail: "dev.keryx.agent: exited 113"))
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        let answer = await model.startAnswering()
        guard case let .failure(error) = answer else {
            Issue.record("expected a failure")
            return
        }
        #expect(error.details == "dev.keryx.agent: exited 113")
        #expect(model.startFailure == error)
    }

    /// Quitting always runs the stop, whatever is installed — but the two questions
    /// ("do we run it" and "do we say it stops answering calls") are answered
    /// separately (I2).
    @Test func quitRunsServiceStopWheneverKeryxIsKeptAnswering() {
        let client = StubClient()
        let model = makeModel(client: client)
        #expect(!model.quitRunsServiceStop, "no client yet")
    }
}
