import Foundation
import Testing
@testable import KeryxKit

@MainActor
@Suite struct DownloadsTests {
    /// Counts what the downloads asked of the system, so every path can be shown to end the
    /// activity it began.
    final class Activities {
        var begun = 0
        var ended = 0
    }

    let activities = Activities()

    func makeDownloads() -> Downloads {
        let activities = activities
        return Downloads(beginActivity: { _ in
            activities.begun += 1
            return { activities.ended += 1 }
        })
    }

    func client(_ pull: ManualPull) -> StubClient {
        StubClient { $0.pull = { _ in pull.stream() } }
    }

    func client(events: [PullEvent], throwing error: Error? = nil) -> StubClient {
        StubClient {
            $0.pull = { _ in
                PullStream(AsyncThrowingStream { continuation in
                    for event in events { continuation.yield(event) }
                    continuation.finish(throwing: error)
                })
            }
        }
    }

    @Test func cancelStopsThePullAndKeepsTheRowCancellingUntilItHasStopped() async {
        let downloads = makeDownloads()
        let pull = ManualPull()
        downloads.start("qwen", client: client(pull), onFinish: {})
        pull.send(.progress(done: 10, total: 100))
        #expect(await eventually { downloads.active["qwen"] == .running(done: 10, total: 100) })
        #expect(downloads.isRunning)
        #expect(downloads.current?.name == "qwen")

        downloads.cancel("qwen")
        #expect(pull.stopRequests == 1)
        #expect(downloads.active["qwen"] == .cancelling)
        #expect(!downloads.isRunning)
        #expect(downloads.isBusy)
        #expect(downloads.current?.phase == .cancelling)
        // A second press does nothing more; a late progress line does not bring it back.
        downloads.cancel("qwen")
        #expect(pull.stopRequests == 1)
        pull.send(.progress(done: 11, total: 100))
        try? await Task.sleep(for: .milliseconds(20))
        #expect(downloads.active["qwen"] == .cancelling)

        // The CLI says so and exits: only now does the row go.
        pull.send(.cancelled)
        pull.finish()
        await downloads.settled()
        #expect(downloads.active["qwen"] == nil)
        #expect(!downloads.isBusy)
        #expect(activities.begun == 1 && activities.ended == 1)
    }

    @Test func nothingStartsWhileADownloadIsCancelling() async {
        let downloads = makeDownloads()
        let first = ManualPull()
        let stub = client(first)
        downloads.start("qwen", client: stub, onFinish: {})
        downloads.cancel("qwen")
        // Neither the same model (two writers on one .part) nor another.
        downloads.start("qwen", client: stub, onFinish: {})
        downloads.start("gemma", client: stub, onFinish: {})
        #expect(stub.calls == ["models pull qwen"])
        #expect(downloads.active == ["qwen": .cancelling])

        first.finish()   // killed: no terminal event, and that is not a failure here
        await downloads.settled()
        #expect(downloads.active["qwen"] == nil)

        let second = ManualPull()
        downloads.start("qwen", client: client(second), onFinish: {})
        second.send(.progress(done: 5, total: 10))
        #expect(await eventually { downloads.active["qwen"] == .running(done: 5, total: 10) })
        second.send(.done(path: "/q.gguf"))
        second.finish()
        await downloads.settled()
        #expect(downloads.active["qwen"] == .finished)
    }

    @Test func aDownloadThatFinishesAsItIsCancelledIsFinished() async {
        let downloads = makeDownloads()
        let pull = ManualPull()
        var finished = 0
        downloads.start("qwen", client: client(pull), onFinish: { finished += 1 })
        downloads.cancel("qwen")
        pull.send(.done(path: "/q.gguf"))
        pull.finish()
        await downloads.settled()
        #expect(downloads.active["qwen"] == .finished)
        #expect(finished == 1)
    }

    @Test func aCancelledDownloadThatErrsIsStillJustGone() async {
        let downloads = makeDownloads()
        let pull = ManualPull()
        downloads.start("qwen", client: client(pull), onFinish: {})
        downloads.cancel("qwen")
        pull.finish(throwing: KeryxError.launch("gone"))
        await downloads.settled()
        #expect(downloads.active["qwen"] == nil)
    }

    /// End to end, with a shell standing in for `keryx models pull`: progress, a grandchild
    /// (uv's Python), and `cancelled` on SIGTERM, as the CLI does.
    @Test func cancellingARealPullWaitsForTheWholeGroupToGo() async throws {
        let file = NSTemporaryDirectory() + "keryx-pull-\(UUID().uuidString)"
        defer { try? FileManager.default.removeItem(atPath: file) }
        let script = """
            trap 'echo "{\\"event\\": \\"cancelled\\"}"; exit 130' TERM
            sleep 30 & echo $! > '\(file)'
            echo '{"event": "progress", "done": 1, "total": 4}'
            wait
            """
        var location = KeryxLocation.fixture
        location.executable = "/bin/sh"
        location.prefixArguments = ["-c", script, "keryx"]
        location.checkout = "/tmp"
        location.workingDirectory = "/tmp"
        let client = CLIClient(location: location, runner: ProcessRunner())
        let downloads = makeDownloads()
        downloads.start("qwen", client: client, onFinish: {})
        #expect(await eventually { downloads.active["qwen"] == .running(done: 1, total: 4) })
        let grandchild = try await ProcessRunnerTests.pid(writtenTo: file)

        downloads.cancel("qwen")
        #expect(downloads.active["qwen"] == .cancelling)
        await downloads.settled()
        #expect(downloads.active["qwen"] == nil)
        #expect(await ProcessRunnerTests.isGone(grandchild))
    }

    @Test func thePreviewClientStopsWhenAsked() async {
        let downloads = makeDownloads()
        downloads.start("qwen", client: PreviewClient(pull: .holding(done: 1, total: 4)), onFinish: {})
        #expect(await eventually { downloads.active["qwen"] == .running(done: 1, total: 4) })
        downloads.cancel("qwen")
        await downloads.settled()
        #expect(downloads.active["qwen"] == nil)
    }

    @Test func doneFinishesAndCallsOnFinish() async {
        let downloads = makeDownloads()
        var finished = 0
        downloads.start("qwen", client: client(events: [
            .progress(done: 1, total: 2), .done(path: "/m.gguf"),
        ]), onFinish: { finished += 1 })
        await downloads.settled()
        #expect(downloads.active["qwen"] == .finished)
        #expect(finished == 1)
        #expect(!downloads.isRunning)
        #expect(activities.begun == 1 && activities.ended == 1)
    }

    @Test func adoptedFinishesToo() async {
        let downloads = makeDownloads()
        var finished = 0
        downloads.start("qwen", client: client(events: [.adopted(path: "/m.gguf", from: "/old.gguf")]),
                        onFinish: { finished += 1 })
        await downloads.settled()
        #expect(downloads.active["qwen"] == .finished)
        #expect(finished == 1)
    }

    @Test func previewPullYieldsTheFixtureAndFinishes() async {
        let downloads = makeDownloads()
        var finished = false
        downloads.start("qwen", client: PreviewClient(), onFinish: { finished = true })
        await downloads.settled()
        #expect(downloads.active["qwen"] == .finished)
        #expect(finished)
    }

    @Test func anErrorEventFails() async {
        let downloads = makeDownloads()
        var finished = false
        downloads.start("qwen", client: client(events: [.progress(done: 1, total: 2), .error("Disk full")]),
                        onFinish: { finished = true })
        await downloads.settled()
        #expect(downloads.active["qwen"] == .failed("Disk full"))
        #expect(!finished)
        #expect(downloads.current?.name == "qwen")
        #expect(activities.begun == 1 && activities.ended == 1)
    }

    @Test func aCancelledEventRemovesTheRow() async {
        let downloads = makeDownloads()
        downloads.start("qwen", client: client(events: [.progress(done: 1, total: 2), .cancelled]),
                        onFinish: {})
        await downloads.settled()
        #expect(downloads.active["qwen"] == nil)
        #expect(downloads.current == nil)
        #expect(activities.begun == 1 && activities.ended == 1)
    }

    @Test func aStreamThatStopsWithoutATerminalEventFails() async {
        let downloads = makeDownloads()
        var finished = false
        downloads.start("qwen", client: client(events: [.progress(done: 1, total: 2)]),
                        onFinish: { finished = true })
        await downloads.settled()
        #expect(downloads.active["qwen"] == .failed(
            "The download stopped without saying why. Run keryx doctor in Terminal."))
        #expect(!finished)
        #expect(activities.begun == 1 && activities.ended == 1)
    }

    @Test func aThrownErrorFailsWithItsSentence() async {
        let downloads = makeDownloads()
        downloads.start("qwen", client: client(events: [], throwing: KeryxError.launch("No uv")),
                        onFinish: {})
        await downloads.settled()
        #expect(downloads.active["qwen"] == .failed("No uv"))
        #expect(activities.begun == 1 && activities.ended == 1)
    }

    @Test func oneAtATime() async {
        let downloads = makeDownloads()
        let first = ManualPull()
        let stub = client(first)
        downloads.start("qwen", client: stub, onFinish: {})
        downloads.start("gemma", client: stub, onFinish: {})
        #expect(downloads.active["gemma"] == nil)
        #expect(stub.calls == ["models pull qwen"])
        first.send(.done(path: "/q.gguf"))
        first.finish()
        await downloads.settled()

        downloads.start("gemma", client: client(events: [.done(path: "/g.gguf")]), onFinish: {})
        await downloads.settled()
        #expect(downloads.active["gemma"] == .finished)
        #expect(downloads.current?.name == "gemma")
        #expect(activities.begun == 2 && activities.ended == 2)
    }

    @Test func aFailedDownloadCanBeTriedAgain() async {
        let downloads = makeDownloads()
        downloads.start("qwen", client: client(events: [.error("Network down")]), onFinish: {})
        await downloads.settled()
        #expect(downloads.active["qwen"] == .failed("Network down"))
        downloads.start("qwen", client: client(events: [.done(path: "/q.gguf")]), onFinish: {})
        await downloads.settled()
        #expect(downloads.active["qwen"] == .finished)
    }

}
