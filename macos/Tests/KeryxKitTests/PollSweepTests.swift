import Foundation
import Testing
@testable import KeryxKit

/// The stale Terminal scripts are swept with every status poll while the app runs, not
/// only at launch.
@MainActor
@Suite struct PollSweepTests {
    final class Counter: @unchecked Sendable {
        private let lock = NSLock()
        private var count = 0
        var value: Int { lock.withLock { count } }
        func add() { lock.withLock { count += 1 } }
    }

    @Test func everyPollSweeps() async {
        let sweeps = Counter()
        let client = StubClient()
        let model = AppModel(
            locator: StubLocator(), makeClient: { _ in client },
            preferences: Preferences(defaults: freshDefaults()),
            timing: AppModel.Timing(poll: .milliseconds(5)), sweep: { sweeps.add() })
        await model.start()
        defer { model.stop() }
        #expect(await eventually { sweeps.value >= 1 })
    }

    /// Off the main actor, and never two at once: a slow sweep makes later polls skip theirs.
    @Test func aSweepRunsOffTheMainActorOneAtATime() async {
        let overlap = Overlap()
        let model = AppModel(
            locator: StubLocator(), makeClient: { _ in StubClient() },
            preferences: Preferences(defaults: freshDefaults()),
            timing: AppModel.Timing(poll: .milliseconds(5)), sweep: { overlap.run() })
        await model.start()
        defer { model.stop() }
        #expect(await eventually { overlap.runs >= 2 })
        #expect(overlap.most == 1)
        #expect(!overlap.sawMainThread)
    }

    final class Overlap: @unchecked Sendable {
        private let lock = NSLock()
        private var now = 0
        private var count = 0
        private var peak = 0
        private var main = false
        var runs: Int { lock.withLock { count } }
        var most: Int { lock.withLock { peak } }
        var sawMainThread: Bool { lock.withLock { main } }

        func run() {
            lock.withLock {
                now += 1
                count += 1
                peak = max(peak, now)
                main = main || Thread.isMainThread
            }
            Thread.sleep(forTimeInterval: 0.05)
            lock.withLock { now -= 1 }
        }
    }
}
