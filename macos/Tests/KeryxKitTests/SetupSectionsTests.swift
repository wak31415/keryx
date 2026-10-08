import Foundation
import Testing
@testable import KeryxKit
import KeryxTestSupport
import KeryxPreview

/// "About you", the move from Jarvis and project context, as `CLIClient` runs them (the
/// command line, the one JSON object on stdin, which exits are answers) and as `AppModel`
/// holds them (one run at a time, the migration not stopped once it has begun).
@MainActor @Suite struct SetupSectionsTests {
    typealias Client = SetupClientTests

    // MARK: - About you

    @Test func theProfileQuestionsAreRead() async throws {
        let (client, fake) = Client.client(PreviewData.data("setup-profile.json"))
        let questions = try await client.profileQuestions()
        #expect(Client.words(fake.requests.last) == ["setup", "profile", "--json"])
        #expect(questions.questions.map(\.id) == ["replace", "uses", "other", "facts"])
        #expect(questions.question("uses")?.choices?.map(\.value) == ["email", "coding", "calendar", "research", "cluster"])
        #expect(questions.memory.exists && questions.memory.maxChars == 4000)
        #expect(questions.question("facts")?.default == .array([]))
    }

    @Test func theAnswersAreOneJSONObjectOnStdin() async throws {
        let (client, fake) = Client.client(try Client.data("fixture:setup-profile-apply.json"))
        let answers = ProfileAnswers(replace: true, uses: ["coding"], other: "", facts: ["Works nights."])
        let result = try await client.applyProfile(answers)
        let request = try #require(fake.requests.last)
        #expect(Client.words(request) == ["setup", "profile", "--apply", "--json"])
        let sent = try #require(request.stdin)
        let object = try #require(try JSONSerialization.jsonObject(with: sent) as? [String: Any])
        #expect(object["replace"] as? Bool == true)
        #expect(object["uses"] as? [String] == ["coding"])
        #expect(object["facts"] as? [String] == ["Works nights."])
        #expect(!String(decoding: sent, as: UTF8.self).contains("\n"), "one line")
        #expect(Client.secretFree(request, "Works nights."), "the answers never on argv")
        #expect(result.status == "written" && result.written && result.walked)

        _ = try await client.previewProfile(answers)
        #expect(Client.words(fake.requests.last) == ["setup", "profile", "--preview", "--json"])
    }

    @Test func aRefusedProfileIsAnAnswerWithItsProblems() async throws {
        let (client, _) = Client.client(try Client.data("fixture:setup-profile-refused.json"), exit: 1)
        let result = try await client.applyProfile(ProfileAnswers(facts: ["…"]))
        #expect(result.refused && !result.written)
        #expect(result.problems.map(\.id) == ["facts"])
    }

    @Test func aProfileRefusedWithNoDocumentIsAFailure() async throws {
        let fake = FakeRunner { _ in Client.refused("keryx cannot do that as the service", exit: 1) }
        await #expect(throws: KeryxError.self) {
            _ = try await CLIClient(location: .fixture, runner: fake).applyProfile(ProfileAnswers())
        }
    }

    // MARK: - The move from Jarvis

    @Test func theMigrationPlanTouchesNothing() async throws {
        let (client, fake) = Client.client(PreviewData.data("migrate-plan.json"))
        let plan = try await client.migrationPlan()
        #expect(Client.words(fake.requests.last) == ["migrate", "--json"])
        #expect(plan.ready && !plan.nothingToDo)
        #expect(plan.moving.count == 9, "the socket is removed, not moved")
        #expect(plan.service.stops == "dev.jarvis.agent")
        #expect(PreviewData.migrationPlanConflict.conflicts.first?.message.hasPrefix("A file from before") == true)
        #expect(PreviewData.migrationPlanNothing.nothingToDo && PreviewData.migrationPlanNothing.retire == nil)
    }

    @Test func theMigrationStreamsItsSteps() async throws {
        let fake = FakeRunner()
        fake.lineResponder = { _ in PreviewData.migrateLines }
        var events: [MigrationEvent] = []
        for try await event in CLIClient(location: .fixture, runner: fake).migrate() { events.append(event) }
        #expect(Client.words(fake.requests.last) == ["migrate", "--yes", "--json"])
        #expect(fake.requests.last?.timeout == nil)
        #expect(events.first == .step(step: "move", message: "Moving Keryx's files."))
        #expect(events.last == .done(MigrationDone(summary: "Keryx's files were moved.", nothingToDo: false,
                                                   unitsPending: false)))
    }

    @Test func theMigrationCannotBeStoppedOnceAStepHasCome() async throws {
        let run = ManualRun<MigrationDone>()
        let client = StubClient { $0.migrate = { run.stream() } }
        let model = AppModel.stubbed(client)
        #expect(model.migrate())
        #expect(!model.migrate(), "one at a time")
        #expect(model.canCancelMigration, "nothing has moved yet")

        run.send(.step(step: "stop", message: "Stopping the background service."))
        #expect(await eventually { model.migrationRun.lastStep == "stop" })
        #expect(!model.canCancelMigration)
        model.cancelMigration()
        model.cancelRuns()
        #expect(run.stopRequests == 0, "neither Cancel nor quitting stops it now")

        run.send(.done(MigrationDone(summary: "Keryx's files were moved.", nothingToDo: false, unitsPending: false)))
        run.finish()
        #expect(await eventually { model.migrationRun.result != nil })
        #expect(await eventually { client.calls.contains("setup plan") }, "everything is read again")
    }

    @Test func aMigrationNotYetStartedMayBeStopped() async throws {
        let run = ManualRun<MigrationDone>()
        let model = AppModel.stubbed(StubClient { $0.migrate = { run.stream() } })
        model.migrate()
        model.cancelMigration()
        #expect(run.stopRequests == 1)
        #expect(model.migrationRun.phase == .cancelling)
        run.send(.cancelled)
        run.finish()
        #expect(await eventually { model.migrationRun.phase == nil })
    }

    // MARK: - Project context

    @Test func projectContextIsAskedThenDraftedThenKept() async throws {
        let (client, fake) = Client.client(PreviewData.data("project-context.json"))
        let question = try await client.projectContextQuestion()
        #expect(Client.words(fake.requests.last) == ["setup", "project-context", "--describe", "--json"])
        #expect(question.choices.map(\.value) == ["explore", "folders", "no"])
        #expect(question.agentName == "Claude")

        let drafter = FakeRunner()
        drafter.lineResponder = { _ in PreviewData.projectDraftLines }
        let cli = CLIClient(location: .fixture, runner: drafter)
        var last: ProjectDraftEvent?
        for try await event in cli.draftProjectContext(.explore) { last = event }
        #expect(Client.words(drafter.requests.last) == ["setup", "project-context", "--json", "--explore"])
        guard case let .done(draft)? = last else { Issue.record("no draft"); return }
        #expect(draft.projects.map(\.name) == ["keryx"])
        #expect(draft.facts == ["Works nights."])
        #expect(draft.projects.first?.path == nil)

        for try await _ in cli.draftProjectContext(.folders(["/Users/you/code", "-odd"])) {}
        #expect(Client.words(drafter.requests.last)
            == ["setup", "project-context", "--json", "--folder=/Users/you/code", "--folder=-odd"])
    }

    @Test func whatIsKeptGoesOnStdinWithItsPathsAsNull() async throws {
        let (client, fake) = Client.client(try Client.data("fixture:project-context-apply.json"))
        let keep = ProjectContextKeep(folders: ["/Users/you/code"],
                                      projects: [.init(name: "keryx", path: nil, summary: "The voice assistant.")],
                                      facts: ["Works nights."])
        let result = try await client.applyProjectContext(keep)
        let request = try #require(fake.requests.last)
        #expect(Client.words(request) == ["setup", "project-context", "--apply", "--json"])
        let stdin = try #require(request.stdin)
        let object = try #require(try JSONSerialization.jsonObject(with: stdin) as? [String: Any])
        let projects = try #require(object["projects"] as? [[String: Any]])
        #expect(projects.first?["path"] is NSNull)
        #expect(object["folders"] as? [String] == ["/Users/you/code"])
        #expect(result.kept == ["keryx"] && result.factsAdded == 1 && result.walked)
    }

    @Test func theModelHoldsOneDraftAtATimeAndQuittingStopsIt() async throws {
        let run = ManualRun<ProjectDraft>()
        let client = StubClient { $0.projectDraft = { _ in run.stream() } }
        let model = AppModel.stubbed(client)
        #expect(model.draftProjectContext(.folders(["/a", "/b"])))
        #expect(!model.draftProjectContext(.explore))
        #expect(client.calls.last == "setup project-context --folder ×2")
        #expect(model.isBusy)
        model.cancelRuns()
        #expect(run.stopRequests == 1)
    }

    @Test func applyingReadsTheMemoryAndThePlanAgain() async throws {
        let client = StubClient()
        let model = AppModel.stubbed(client)
        _ = await model.applyProfile(ProfileAnswers(facts: ["Works nights."]))
        #expect(client.calls.suffix(2).sorted() == ["memory", "setup plan"])
        _ = await model.applyProjectContext(ProjectContextKeep(folders: [], projects: [], facts: []))
        #expect(client.calls.contains("setup project-context --apply"))
        #expect(client.piped.count == 2)
    }

    // MARK: - Task L's seams

    @Test func aPlanThatCanDeferIsNearlyReadyWhenAllThatIsLeftWasDeferred() {
        var plan = PreviewData.setupPlanReady
        // The shared fixture now speaks of deferring (Task L has landed); start from an
        // older CLI's shape (neither key printed) before building the scenario by hand.
        for index in plan.steps.indices {
            plan.steps[index].deferrable = nil
            plan.steps[index].deferred = nil
        }
        #expect(!plan.speaksOfDeferring && !plan.nearlyReady, "an older CLI says nothing of deferring")
        plan.ready = false
        for index in plan.steps.indices {
            plan.steps[index].deferrable = ["phone", "service"].contains(plan.steps[index].id)
            plan.steps[index].deferred = false
        }
        guard let phone = plan.steps.firstIndex(where: { $0.id == "phone" }) else { return }
        plan.steps[phone].state = "needed"
        #expect(plan.speaksOfDeferring && !plan.nearlyReady, "still to do, not left for later")
        plan.steps[phone].deferred = true
        #expect(plan.nearlyReady)
        let marked = try? JSON.decoder.decode(SetupMarkResult.self, from: Data(#"{"id": "phone", "walked": true, "deferred": true}"#.utf8))
        #expect(marked?.deferred == true)
    }

    @Test func anAgentsTestTaskEndsWithItsVerdict() {
        let failed = StreamEvent<AgentSmoked>.parse(
            #"{"event": "done", "ok": false, "summary": "Claude couldn't run a test task.", "detail": "It said: no."}"#)
        #expect(failed == .done(AgentSmoked(ok: false, summary: "Claude couldn't run a test task.", detail: "It said: no.")))
        #expect(failed?.isTerminal == true)
        #expect(StreamEvent<AgentSmoked>.parse(#"{"event": "done", "summary": "x"}"#) == nil)
        #expect(CommandLog.describe(executable: "/x/keryx", arguments: ["auth", "smoke", "local", "--json"])
            == "keryx auth smoke local --json")
    }

    // MARK: - Cloudflare Tunnel, with Homebrew

    @Test func homebrewIsFoundOnThePathOrInItsOwnPlaces() {
        let none = CLIClient(location: .fixture, runner: FakeRunner(), files: FakeFileSystem())
        #expect(none.homebrew() == nil)
        let own = CLIClient(location: .fixture, runner: FakeRunner(),
                            files: FakeFileSystem(executables: ["/usr/local/bin/brew"]))
        #expect(own.homebrew() == "/usr/local/bin/brew")
        let onPath = CLIClient(location: .fixture, runner: FakeRunner(),
                               files: FakeFileSystem(executables: ["/opt/homebrew/bin/brew", "/Users/you/.local/bin/brew"]))
        #expect(onPath.homebrew() == "/Users/you/.local/bin/brew", "the PATH Keryx runs with comes first")
        // A relative PATH entry is never a place to run brew from.
        var relative = KeryxLocation.fixture
        relative.environment["PATH"] = "bin:/usr/bin"
        let skipped = CLIClient(location: relative, runner: FakeRunner(),
                                files: FakeFileSystem(executables: ["bin/brew", "/usr/local/bin/brew"]))
        #expect(skipped.homebrew() == "/usr/local/bin/brew")
    }

    @Test func whatBrewInstalledIsFoundWhereBrewWasFound() {
        // brew from Homebrew's own folder, not on Keryx's PATH: cloudflared is found beside it.
        let client = CLIClient(location: .fixture, runner: FakeRunner(),
                               files: FakeFileSystem(executables: ["/opt/homebrew/bin/brew", "/opt/homebrew/bin/cloudflared"]))
        #expect(client.homebrew() == "/opt/homebrew/bin/brew")
        #expect(client.cloudflaredInstalled())
        #expect(!CLIClient(location: .fixture, runner: FakeRunner(), files: FakeFileSystem()).cloudflaredInstalled())
    }

    @Test func anInstallThatWorkedOutsideKeryxsPathReadsAsWorked() async {
        let client = StubClient {
            $0.homebrew = "/opt/homebrew/bin/brew"
            $0.cloudflaredInstalled = true
            $0.phoneTunnel = .success(PreviewData.phoneTunnelUnset)
        }
        let model = AppModel.stubbed(client)
        model.installTunnel()
        await model.tunnelInstall.settled()
        #expect(model.tunnelInstall.phase == .finished)
        #expect(model.tunnelOutsideKeryxPath, "and the frame says why Keryx doesn't see it")
    }

    @Test func installingTheTunnelIsBrewInstallCloudflaredAndNothingElse() async throws {
        let fake = FakeRunner()
        fake.lineResponder = { _ in ["==> Fetching cloudflared", "", "🍺  cloudflared was installed"] }
        let client = CLIClient(location: .fixture, runner: fake)
        var lines: [String] = []
        for try await line in client.installCloudflared(brew: "/opt/homebrew/bin/brew") { lines.append(line) }
        let request = try #require(fake.requests.last)
        #expect(request.executable == "/opt/homebrew/bin/brew")
        #expect(request.arguments == ["install", "cloudflared"])
        #expect(request.environment["KERYX_ACTOR"] == nil && request.timeout == nil)
        #expect(request.mergesStderr, "Homebrew says its errors on stderr")
        #expect(lines == ["==> Fetching cloudflared", "🍺  cloudflared was installed"], "blank lines dropped")
        #expect(CommandLog.describe(executable: request.executable, arguments: request.arguments) == "brew …")
    }

    @Test func theInstallWorkedOnlyIfKeryxThenFindsTheTunnel() async throws {
        let install = ProgramInstall()
        install.start(StoppableStream(AsyncThrowingStream { $0.yield("==> Pouring"); $0.finish() })) { true }
        await install.settled()
        #expect(install.phase == .finished)
        let failed = ProgramInstall()
        // As the runner hands it over with stderr merged: Homebrew's error after its progress.
        failed.start(StoppableStream(AsyncThrowingStream {
            $0.yield("==> Fetching cloudflared")
            $0.yield("Error: no bottle available")
            $0.finish()
        })) { false }
        await failed.settled()
        #expect(failed.phase == .failed(StreamFailure(ProgramInstall.didNotInstall,
                                                      detail: "==> Fetching cloudflared\nError: no bottle available")))
    }

    @Test func aCancelledInstallLeavesNothingAndQuittingStopsIt() async throws {
        let (stream, continuation) = AsyncThrowingStream<String, Error>.makeStream()
        let stopped = Counter()
        let client = StubClient {
            $0.homebrew = "/opt/homebrew/bin/brew"
            $0.brewInstall = { StoppableStream(stream, stop: { stopped.add(); continuation.finish() }) }
        }
        let model = AppModel.stubbed(client)
        model.installTunnel()
        #expect(model.tunnelInstall.isRunning && model.isBusy)
        model.cancelRuns()
        #expect(stopped.count == 1)
        await model.tunnelInstall.settled()
        #expect(model.tunnelInstall.phase == nil)
        #expect(!client.calls.contains("phone tunnel"), "a cancelled install asks nothing after")
    }

    @Test func theAgentsTestRunsOnlyWhereTheClientCan() {
        let model = AppModel.stubbed(StubClient())
        #expect(!model.smokeTest("claude"), "StubClient does not adopt AgentSmokeTesting")
        #expect(model.smokeRun.phase == nil)
    }

    // MARK: - The log

    @Test func theLogNamesTheVerbsAndFlagsNeverAFolder() {
        #expect(CommandLog.describe(executable: "/x/keryx", arguments: ["migrate", "--yes", "--json"])
            == "keryx migrate --yes --json")
        #expect(CommandLog.describe(executable: "/x/keryx", arguments: ["setup", "profile", "--apply", "--json"])
            == "keryx setup profile --apply --json")
        #expect(CommandLog.describe(executable: "/x/keryx",
                                    arguments: ["setup", "project-context", "--json", "--folder=/Users/you/secret"])
            == "keryx setup project-context --json --folder=…")
        #expect(CommandLog.describe(executable: "/x/keryx",
                                    arguments: ["setup", "project-context", "--describe", "--json"])
            == "keryx setup project-context --describe --json")
    }
}

extension AppModel {
    /// A model already holding `client`, with nothing polled.
    @MainActor static func stubbed(_ client: StubClient) -> AppModel {
        let model = AppModel(locator: StubLocator(), makeClient: { _ in client }, preferences: Preferences(defaults: freshDefaults()),
                             timing: Timing(poll: nil))
        model.client = client
        model.started = true
        return model
    }
}

/// A count shared with a `@Sendable` closure.
final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0
    var count: Int { lock.withLock { value } }
    func add() { lock.withLock { value += 1 } }
}
