import Foundation

/// Waits for something done in the background — a run's progress, a SwiftUI update, a
/// write — rather than for a fixed time. It returns as soon as `condition` holds; the
/// generous timeout is for a main actor kept busy by the suites that render views (every
/// test target shares one), never a wait in the passing case.
public enum Waiting {
    @MainActor
    public static func until(timeout: Duration = .seconds(180), _ condition: @MainActor () -> Bool) async -> Bool {
        let clock = ContinuousClock()
        let deadline = clock.now + timeout
        while clock.now < deadline {
            if condition() { return true }
            try? await Task.sleep(for: .milliseconds(2))
        }
        return condition()
    }
}
