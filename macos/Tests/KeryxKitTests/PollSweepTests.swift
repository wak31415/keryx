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
}
