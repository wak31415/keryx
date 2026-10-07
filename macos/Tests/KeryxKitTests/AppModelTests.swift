import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview

@MainActor
@Suite struct AppModelTests {
    /// Short enough that nothing waits on a real 30 s, 2 s or 3 s.
    static let fast = AppModel.Timing(poll: .milliseconds(5), afterModelServer: .milliseconds(1),
                                      afterRestart: .milliseconds(1))
    /// No poll at all.
    static let noPoll = AppModel.Timing(poll: nil, afterModelServer: .milliseconds(1),
                                        afterRestart: .milliseconds(1))

    func makeModel(
        locator: StubLocator = StubLocator(), client: StubClient = StubClient(),
        preferences: Preferences = Preferences(defaults: freshDefaults()),
        timing: AppModel.Timing = noPoll, made: Made? = nil
    ) -> AppModel {
        AppModel(locator: locator, makeClient: { location in
            made?.record(location)
            return client
        }, preferences: preferences, timing: timing)
    }

    final class Made: @unchecked Sendable {
        private let lock = NSLock()
        private var locations: [KeryxLocation] = []
        var all: [KeryxLocation] { lock.withLock { locations } }
        func record(_ location: KeryxLocation) { lock.withLock { locations.append(location) } }
    }

    // MARK: - Starting

    @Test func startLocatesThenLoadsStatus() async {
        let locator = StubLocator()
        let client = StubClient()
        let made = Made()
        let preferences = Preferences(defaults: freshDefaults())
        preferences.keryxFolder = "/Users/you/code/keryx"
        let model = makeModel(locator: locator, client: client, preferences: preferences, made: made)
        #expect(model.menuState == .locating)

        await model.start()
        defer { model.stop() }

        #expect(locator.askedWith == ["/Users/you/code/keryx"])
        #expect(made.all == [.fixture])
        #expect(model.location.value == .fixture)
        #expect(model.client != nil)
        #expect(Set(client.calls) == ["status", "doctor"])
        #expect(model.status.value == PreviewData.status)
        #expect(model.status.updatedAt != nil)
        #expect(!model.status.isLoading)
        #expect(model.doctor.value == PreviewData.doctor)
        #expect(model.menuState == .attention("2 results not yet told"))
    }

    @Test func notFoundStopsBeforeAnyCommand() async {
        let failure = LocateFailure(lookedAt: ["No LaunchAgent.", "No keryx on the login shell's PATH."])
        let made = Made()
        let model = makeModel(locator: StubLocator(.failure(failure)), made: made)
        await model.start()
        defer { model.stop() }
        #expect(made.all.isEmpty)
        #expect(model.client == nil)
        #expect(model.location.error == .notFound(failure))
        #expect(model.menuState == .notFound)
        await model.refresh(.all)
        #expect(made.all.isEmpty)
        #expect(model.status.value == nil && model.status.error == nil)
    }

    @Test func startTwiceLocatesOnce() async {
        let locator = StubLocator()
        let model = makeModel(locator: locator)
        await model.start()
        await model.start()
        defer { model.stop() }
        #expect(locator.askedWith.count == 1)
    }

    @Test func statusMigrationMakesMenuStateNeedsMigration() async {
        let client = StubClient { $0.status = .success(PreviewData.statusMigration) }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        #expect(model.menuState == .needsMigration)
    }

    @Test func aMigrationErrorAlsoNeedsMigration() async {
        let client = StubClient { $0.status = .failure(.migrationRequired("run keryx migrate first")) }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        #expect(model.status.error == .migrationRequired("run keryx migrate first"))
        #expect(model.menuState == .needsMigration)
    }

    @Test func thePollLoopPolls() async {
        let client = StubClient()
        let model = makeModel(client: client, timing: Self.fast)
        await model.start()
        defer { model.stop() }
        // One tick is enough to show the loop runs; what a tick does is the test above's.
        #expect(await eventually { client.calls.filter { $0 == "status" }.count >= 2 })
    }

    @Test func errorsAreStoredNotThrown() async {
        let client = StubClient { $0.models = .failure(.timedOut(command: "keryx models list")) }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.refresh(.models)
        #expect(model.models.error == .timedOut(command: "keryx models list"))
        #expect(model.models.value == nil)
        #expect(!model.models.isLoading)

        client.update { $0.models = .success(PreviewData.models) }
        await model.refresh(.models)
        #expect(model.models.value == PreviewData.models)
        #expect(model.models.error == nil)
    }

    @Test func refreshAllLoadsEveryDocument() async {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.refresh(.all)
        #expect(model.tasks.value == PreviewData.tasks.tasks)
        #expect(model.usage.value == PreviewData.usage)
        #expect(model.models.value == PreviewData.models)
        #expect(model.config.value == PreviewData.config)
        #expect(model.approvals.value == PreviewData.approvals)
        #expect(model.memory.value == PreviewData.memory)
    }

    @Test func housekeepingAsksForInternalTasks() async {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        model.includeHousekeeping = true
        await model.refresh(.tasks)
        #expect(client.calls.last == "tasks list --internal")
    }

    @Test func loadTaskKeepsADetailPerID() async {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.loadTask(42)
        #expect(client.calls.last == "tasks show 42")
        #expect(model.taskDetail[42]?.value == PreviewData.taskDetail)
        client.update { $0.task = .failure(.failed(command: "keryx tasks show", exitCode: 1, message: "no task 7")) }
        await model.loadTask(7)
        #expect(model.taskDetail[7]?.error?.reported == "no task 7")
        #expect(model.taskDetail[42]?.value == PreviewData.taskDetail)
    }

    // MARK: - Writes

    @Test func configWriteRefreshesConfig() async {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        let row = PreviewData.config[0]
        let error = await model.set(row, to: "new")
        #expect(error == nil)
        #expect(Array(client.calls.suffix(2)) == ["config set \(row.key) new", "config list"])
        #expect(model.config.value == PreviewData.config)
        #expect(model.configNeedsRestart)
    }

    @Test func secretAndUnsetRefreshConfigToo() async {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        #expect(await model.setSecret("OPENAI_API_KEY", value: "sk-secret") == nil)
        #expect(Array(client.calls.suffix(2)) == ["config set OPENAI_API_KEY --stdin", "config list"])
        #expect(await model.unset("OPENAI_VOICE") == nil)
        #expect(Array(client.calls.suffix(2)) == ["config unset OPENAI_VOICE", "config list"])
        #expect(await model.unset(keys: ["ISSUE_REPORTING", "ISSUE_REPO"]) == nil)
        #expect(Array(client.calls.suffix(2)) == ["config unset ISSUE_REPORTING ISSUE_REPO", "config list"])
        let before = client.calls.count
        #expect(await model.unset(keys: []) == nil)
        #expect(client.calls.count == before, "nothing to unset runs nothing")
        #expect(model.configNeedsRestart)
    }

    @Test func aRefusedWriteReturnsItsErrorAndChangesNothing() async {
        let refusal = KeryxError.failed(command: "keryx config set", exitCode: 1, message: "not a port")
        let client = StubClient { $0.write = refusal }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        let error = await model.set(PreviewData.config[0], to: "x")
        #expect(error == refusal)
        #expect(client.calls.last == "config set \(PreviewData.config[0].key) x")
        #expect(!model.configNeedsRestart)
    }

    @Test func writesWithoutKeryxSayNotFound() async {
        let failure = LocateFailure(lookedAt: ["Nothing."])
        let model = makeModel(locator: StubLocator(.failure(failure)))
        await model.start()
        defer { model.stop() }
        #expect(await model.unset("X") == .notFound(failure))
        #expect(await model.restart(force: false) == .notFound(failure))
    }

    @Test func useSetsNeedsRestart() async {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        let error = await model.use(model: "qwen3-coder-30b", defaultAgent: true)
        #expect(error == nil)
        #expect(client.calls.contains("models use qwen3-coder-30b --default-agent"))
        #expect(client.calls.suffix(2).sorted() == ["config list", "models list"])
        #expect(model.configNeedsRestart)
        #expect(model.models.value == PreviewData.models)
    }

    @Test func useThatNeedsNoRestartSaysSo() async {
        let client = StubClient { $0.use = .success(StubClient.useResult(restartNeeded: false)) }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        #expect(await model.use(model: "qwen3-coder-30b", defaultAgent: false) == nil)
        #expect(client.calls.contains("models use qwen3-coder-30b"))
        #expect(!model.configNeedsRestart)
    }

    @Test func restartClearsNeedsRestartAndRefreshesStatus() async {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        _ = await model.set(PreviewData.config[0], to: "x")
        #expect(model.configNeedsRestart)
        #expect(await model.restart(force: true) == nil)
        #expect(Array(client.calls.suffix(2)) == ["restart --force", "status"])
        #expect(!model.configNeedsRestart)
    }

    @Test func aRefusedRestartKeepsNeedsRestart() async {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        _ = await model.set(PreviewData.config[0], to: "x")
        client.update { $0.write = .liveCalls(2) }
        #expect(await model.restart(force: false) == .liveCalls(2))
        #expect(client.calls.last == "restart")
        #expect(model.configNeedsRestart)
    }

    @Test func escalationRefreshesApprovalsAndStatus() async {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        #expect(await model.setEscalation(enabled: false) == nil)
        #expect(client.calls.contains("approvals --disable"))
        #expect(client.calls.suffix(2).sorted() == ["approvals", "status"])
        #expect(!model.configNeedsRestart)
    }

    @Test func startModelServerRefreshesModels() async {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        #expect(await model.startModelServer() == nil)
        #expect(Array(client.calls.suffix(2)) == ["install-launchd.sh --llm", "models list"])
    }

    @Test func aDownloadThatFinishesRefreshesModels() async {
        let client = StubClient {
            $0.pull = { _ in PullStream(AsyncThrowingStream { $0.yield(.done(path: "/m.gguf")); $0.finish() }) }
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        model.download("qwen")
        #expect(await eventually { model.models.value != nil })
        #expect(client.calls.suffix(2) == ["models pull qwen", "models list"])
    }

    // MARK: - Relocating

    @Test func relocateUsesTheNewPreferenceAndForgetsTheOldKeryx() async {
        let failure = LocateFailure(lookedAt: ["Nothing at /nowhere."])
        let locator = StubLocator()
        let preferences = Preferences(defaults: freshDefaults())
        let model = makeModel(locator: locator, preferences: preferences)
        await model.start()
        defer { model.stop() }
        await model.refresh(.config)
        #expect(model.config.value != nil)

        preferences.keryxFolder = "/nowhere"
        locator.answer(.failure(failure))
        await model.relocate()
        #expect(locator.askedWith == [nil, "/nowhere"])
        #expect(model.client == nil)
        #expect(model.menuState == .notFound)
        #expect(model.status.value == nil)
        #expect(model.config.value == nil)

        preferences.keryxFolder = nil
        locator.answer(.success(.fixture))
        await model.relocate()
        #expect(model.client != nil)
        #expect(model.status.value == PreviewData.status)
    }

    // MARK: - Previews

    @Test func previewModelLoadsEverythingFromPreviewData() {
        let model = AppModel.preview()
        #expect(model.location.value == .fixture)
        #expect(model.status.value == PreviewData.status)
        // Housekeeping is off, as the CLI hides internal tasks without --internal.
        #expect(model.tasks.value == PreviewData.tasks.tasks.filter { !$0.internal })
        #expect(model.tasks.value?.count == PreviewData.tasks.tasks.count - 1)
        #expect(model.taskDetail[PreviewData.taskDetail.task.id]?.value == PreviewData.taskDetail)
        #expect(model.models.value == PreviewData.models)
        #expect(model.memory.value == PreviewData.memory)
    }

    @Test func previewClientOverrides() async throws {
        let client = PreviewClient(
            status: .success(PreviewData.statusMigration), tasks: .success([]),
            models: .failure(.timedOut(command: "keryx models list")))
        #expect(try await client.status() == PreviewData.statusMigration)
        #expect(try await client.tasks(includeInternal: false, limit: 10).isEmpty)
        await #expect(throws: KeryxError.timedOut(command: "keryx models list")) {
            try await client.models()
        }
        let model = AppModel.preview(client: client)
        #expect(model.menuState == .needsMigration)
        #expect(model.models.error == .timedOut(command: "keryx models list"))
    }

    @Test func previewModelNotFound() {
        let model = AppModel.preview(location: .failure(LocateFailure(lookedAt: ["Nothing."])))
        #expect(model.menuState == .notFound)
        #expect(model.client == nil)
    }

    @Test func previewPullCanHoldAtAProgressValue() async {
        let client = PreviewClient(pull: .holding(done: 4_200_000_000, total: 8_100_000_000))
        let downloads = Downloads()
        downloads.start("gemma", client: client, onFinish: {})
        #expect(await eventually {
            downloads.active["gemma"] == .running(done: 4_200_000_000, total: 8_100_000_000)
        })
        downloads.cancel("gemma")
        await downloads.settled()
    }

    @Test func previewPullReplaysTheFixtureLines() async throws {
        let events = PreviewData.pullLines.compactMap(PullEvent.parse)
        var seen: [PullEvent] = []
        for try await event in PreviewClient().pull(model: "qwen") { seen.append(event) }
        #expect(seen == events)
        #expect(!seen.isEmpty)
    }
}

/// What each poll reads, with no clock and no main actor: the render suite holds the main
/// actor for most of a full run, so a test that waited on poll after poll waited it out.
@Suite struct PollTests {
    @Test func eachPollReadsTheStatusAndEveryTenthTheDoctor() {
        let polls = (1...25).map(AppModel.parts(forPoll:))
        #expect(polls.allSatisfy { $0.first == .status })
        #expect(polls.indices.filter { polls[$0].contains(.doctor) }.map { $0 + 1 } == [10, 20])
        #expect(AppModel.parts(forPoll: 9) == [.status])
        #expect(AppModel.parts(forPoll: 10) == [.status, .doctor])
    }
}
