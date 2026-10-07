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
                AsyncThrowingStream { continuation in
                    for event in events { continuation.yield(event) }
                    continuation.finish(throwing: error)
                }
            }
        }
    }

    @Test func cancelPullResetsRow() async {
        let downloads = makeDownloads()
        let pull = ManualPull()
        downloads.start("qwen", client: client(pull), onFinish: {})
        pull.send(.progress(done: 10, total: 100))
        #expect(await eventually { downloads.active["qwen"] == .running(done: 10, total: 100) })
        #expect(downloads.isRunning)
        #expect(downloads.current?.name == "qwen")

        downloads.cancel("qwen")
        #expect(downloads.active["qwen"] == nil)
        #expect(!downloads.isRunning)
        await downloads.settled()
        #expect(pull.wasTerminated)
        #expect(downloads.active["qwen"] == nil)
        #expect(activities.begun == 1 && activities.ended == 1)
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

    @Test func cancellingThenStartingAgainKeepsTheNewRow() async {
        let downloads = makeDownloads()
        let first = ManualPull()
        downloads.start("qwen", client: client(first), onFinish: {})
        downloads.cancel("qwen")
        let second = ManualPull()
        downloads.start("qwen", client: client(second), onFinish: {})
        second.send(.progress(done: 5, total: 10))
        #expect(await eventually { downloads.active["qwen"] == .running(done: 5, total: 10) })
        second.finish()
        await downloads.settled()
        #expect(downloads.active["qwen"] == .failed(
            "The download stopped without saying why. Run keryx doctor in Terminal."))
    }
}
