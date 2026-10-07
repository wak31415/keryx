import Darwin
import Foundation
import Testing
@testable import KeryxKit

/// Every child a runner spawns is listed until its group is closed, and quitting stops them
/// all there: a one-shot run, a stream, a group that ignores SIGTERM. Each test has a
/// registry of its own, so stopping all never reaches another test's children.
@Suite final class ChildRegistryTests: Sendable {
    let leftovers = Leftovers()

    deinit {
        leftovers.clean()
    }

    func runner(_ registry: ChildRegistry, grace: Duration = .milliseconds(300)) -> ProcessRunner {
        ProcessRunner(grace: grace, registry: registry, observingChild: leftovers.add)
    }

    @Test func aFinishedRunIsNoLongerListed() async throws {
        let registry = ChildRegistry()
        let result = try await runner(registry).run(.init(executable: "/usr/bin/true", timeout: .seconds(5)))
        #expect(result.exitCode == 0)
        #expect(await eventually { registry.count == 0 })
    }

    @Test func stoppingAllEndsARunAndAStreamAndWaitsForTheirGroups() async throws {
        let registry = ChildRegistry()
        let runner = runner(registry)
        // A one-shot write still going (a PIN, a secret, an installer)…
        let write = Task { try await runner.run(.init(executable: "/bin/sh", arguments: ["-c", "sleep 30"],
                                                      timeout: nil)) }
        // …and a stream nobody has stopped.
        let stream = runner.lines(.init(executable: "/bin/sh", arguments: ["-c", "echo up; sleep 30"], timeout: nil))
        let reading = Task { () -> [String] in
            var lines: [String] = []
            for try await line in stream { lines.append(line) }
            return lines
        }
        #expect(await eventually { registry.count == 2 })
        let started = ContinuousClock.now
        let survivors = await registry.stopAll()
        #expect(survivors.isEmpty)
        #expect(ContinuousClock.now - started < .seconds(10))
        #expect(registry.count == 0)
        // Each group was signalled, and each leader reaped: the stop was confirmed, not assumed.
        for child in leftovers.all {
            #expect(child.events.first == .signalled(SIGTERM))
            #expect(await eventually { child.events.contains(.reaped) })
        }
        _ = try? await write.value
        _ = try? await reading.value
    }

    @Test func aGroupThatIgnoresSigtermIsKilledBeforeStopAllReturns() async throws {
        let registry = ChildRegistry()
        let file = NSTemporaryDirectory() + "keryx-registry-\(UUID().uuidString)"
        defer { try? FileManager.default.removeItem(atPath: file) }
        let write = Task {
            try await runner(registry).run(.init(executable: "/bin/sh",
                arguments: ["-c", "trap '' TERM; sleep 30 & echo $! > \"$0\"; wait", file], timeout: nil))
        }
        let grandchild = try await ProcessRunnerTests.pid(writtenTo: file)
        #expect(await registry.stopAll().isEmpty)
        #expect(leftovers.all.first?.events.contains(.signalled(SIGKILL)) == true)
        // Gone by the time stopAll returned: only launchd's reaping of the orphan may lag.
        #expect(await ProcessRunnerTests.isGone(grandchild))
        _ = try? await write.value
    }

    @Test func theDeadlineAnswersFirstWhenTheStopTakesLonger() async throws {
        let registry = ChildRegistry()
        let file = NSTemporaryDirectory() + "keryx-registry-\(UUID().uuidString)"
        defer { try? FileManager.default.removeItem(atPath: file) }
        let write = Task {
            try await runner(registry, grace: .seconds(2)).run(.init(executable: "/bin/sh",
                arguments: ["-c", "trap '' TERM; echo $$ > \"$0\"; sleep 30", file], timeout: nil))
        }
        // Only once the trap is set: a SIGTERM before it would end the group at once.
        _ = try await ProcessRunnerTests.pid(writtenTo: file)
        #expect(registry.count == 1)
        let answer = await registry.stopAll(within: .milliseconds(100))
        #expect(answer == nil, "the limit came first")
        // The stop goes on behind it, and still ends the group.
        #expect(await eventually { registry.count == 0 })
        _ = try? await write.value
    }

    @Test func withNothingListedStopAllReturnsAtOnce() async {
        #expect(await ChildRegistry().stopAll(within: .seconds(5)) == [])
    }
}
