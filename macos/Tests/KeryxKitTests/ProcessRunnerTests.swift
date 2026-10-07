import Foundation
import Testing
@testable import KeryxKit

@Suite struct ProcessRunnerTests {
    let runner = ProcessRunner()

    @Test func capturesStdoutStderrAndExitCode() async throws {
        let result = try await runner.run(.init(executable: "/bin/sh",
            arguments: ["-c", "echo out; echo err >&2; exit 3"]))
        #expect(result.stdoutText == "out\n")
        #expect(result.stderrText == "err\n")
        #expect(result.exitCode == 3)
    }

    @Test func passesStdinAndEnvironmentAndCwd() async throws {
        let result = try await runner.run(.init(executable: "/bin/sh",
            arguments: ["-c", "cat; printf %s \"$FOO\"; pwd"], environment: ["FOO": "bar"],
            workingDirectory: "/tmp", stdin: Data("in-".utf8)))
        #expect(result.stdoutText == "in-bar/private/tmp\n" || result.stdoutText == "in-bar/tmp\n")
    }

    @Test func largeOutputDoesNotDeadlock() async throws {
        let result = try await runner.run(.init(executable: "/bin/sh",
            arguments: ["-c", "head -c 1000000 /dev/zero | tr '\\0' a; head -c 200000 /dev/zero >&2"]))
        #expect(result.stdout.count == 1_000_000)
        #expect(result.stderr.count == 200_000)
    }

    @Test func timesOut() async throws {
        await #expect(throws: RunnerError.timedOut) {
            _ = try await runner.run(.init(executable: "/bin/sh", arguments: ["-c", "sleep 5"],
                                           timeout: .milliseconds(200)))
        }
    }

    @Test func timeoutHoldsWhileAGrandchildKeepsThePipesOpen() async throws {
        // The child exits at once; the backgrounded grandchild keeps stdout and stderr
        // open for 5 s. The deadline must still bound the wait.
        let started = ContinuousClock.now
        await #expect(throws: RunnerError.timedOut) {
            _ = try await runner.run(.init(executable: "/bin/sh",
                arguments: ["-c", "(sleep 5 &) ; echo hi"], timeout: .milliseconds(300)))
        }
        #expect(ContinuousClock.now - started < .seconds(1.5))
    }

    @Test func shortRunWithNoOutput() async throws {
        let result = try await runner.run(.init(executable: "/usr/bin/true", timeout: .seconds(5)))
        #expect(result.exitCode == 0)
        #expect(result.stdout.isEmpty)
        #expect(result.stderr.isEmpty)
    }

    @Test func aFinishedRunIsFreed() async throws {
        let probe = WeakProbe()
        let runner = ProcessRunner(observingCollector: { probe.object = $0 })
        let result = try await runner.run(.init(executable: "/bin/sh",
            arguments: ["-c", "echo hi; echo err >&2"], timeout: .seconds(5)))
        #expect(result.stdoutText == "hi\n")
        #expect(probe.wasObserved)
        #expect(await probe.isFreed())
    }

    @Test func aTimedOutRunIsFreed() async throws {
        let probe = WeakProbe()
        let runner = ProcessRunner(observingCollector: { probe.object = $0 })
        await #expect(throws: RunnerError.timedOut) {
            _ = try await runner.run(.init(executable: "/bin/sh",
                arguments: ["-c", "(sleep 5 &) ; echo hi"], timeout: .milliseconds(200)))
        }
        #expect(probe.wasObserved)
        #expect(await probe.isFreed())
    }

    @Test func aLaunchFailureIsFreed() async throws {
        let probe = WeakProbe()
        let runner = ProcessRunner(observingCollector: { probe.object = $0 })
        await #expect(throws: RunnerError.self) {
            _ = try await runner.run(.init(executable: "/nonexistent/keryx", timeout: .seconds(5)))
        }
        #expect(probe.wasObserved)
        #expect(await probe.isFreed())
    }

    @Test func missingExecutableIsLaunchFailed() async {
        await #expect(throws: RunnerError.self) {
            _ = try await runner.run(.init(executable: "/nonexistent/keryx"))
        }
    }

    @Test func emptyEnvironmentDoesNotInheritTheParents() async throws {
        setenv("KERYX_TASK7_MARKER", "present", 1)
        defer { unsetenv("KERYX_TASK7_MARKER") }
        let result = try await runner.run(.init(executable: "/usr/bin/env", environment: [:]))
        #expect(!result.stdoutText.contains("KERYX_TASK7_MARKER"))
    }

    @Test func linesDrainsStderrWithoutBlocking() async throws {
        var got: [String] = []
        for try await line in runner.lines(.init(executable: "/bin/sh",
            arguments: ["-c", "head -c 200000 /dev/zero | tr '\\0' e >&2; echo done"],
            timeout: nil)) { got.append(line) }
        #expect(got == ["done"])
    }

    @Test func streamsLinesAndCancellationKillsTheChild() async throws {
        var got: [String] = []
        for try await line in runner.lines(.init(executable: "/bin/sh",
            arguments: ["-c", "echo a; echo b"], timeout: nil)) { got.append(line) }
        #expect(got == ["a", "b"])
        let task = Task { for try await _ in runner.lines(.init(executable: "/bin/sh",
            arguments: ["-c", "while true; do echo x; sleep 0.05; done"], timeout: nil)) {} }
        try await Task.sleep(for: .milliseconds(200)); task.cancel()
        _ = await task.result   // must return promptly, not hang
    }
}

/// Holds the collector of one run weakly. The last strong reference may be dropped by a block
/// still finishing on the collector's queue just after the continuation resumes, so
/// `isFreed` gives it up to a second rather than asserting at one instant.
private final class WeakProbe: @unchecked Sendable {
    private let lock = NSLock()
    private weak var _object: AnyObject?
    private var observed = false

    var object: AnyObject? {
        get { lock.lock(); defer { lock.unlock() }; return _object }
        set { lock.lock(); _object = newValue; observed = true; lock.unlock() }
    }

    var wasObserved: Bool { lock.lock(); defer { lock.unlock() }; return observed }

    func isFreed() async -> Bool {
        for _ in 0..<100 {
            if object == nil { return true }
            try? await Task.sleep(for: .milliseconds(10))
        }
        return object == nil
    }
}
