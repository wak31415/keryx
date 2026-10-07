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

    // MARK: - Process groups

    @Test func eachChildLeadsAProcessGroupOfItsOwn() async throws {
        let result = try await runner.run(.init(executable: "/bin/sh",
            arguments: ["-c", "echo $$; ps -o pgid= -p $$"], timeout: .seconds(5)))
        let words = result.stdoutText.split(whereSeparator: \.isWhitespace).map(String.init)
        #expect(words.count == 2)
        #expect(words.first == words.last)          // its pgid is its own pid
        #expect(words.first != String(getpgrp()))   // not ours
    }

    @Test func cancellingARunStopsTheGrandchildToo() async throws {
        let file = NSTemporaryDirectory() + "keryx-grandchild-\(UUID().uuidString)"
        defer { try? FileManager.default.removeItem(atPath: file) }
        let run = Task {
            try await runner.run(.init(executable: "/bin/sh",
                arguments: ["-c", "sleep 30 & echo $! > \"$0\"; wait", file], timeout: .seconds(30)))
        }
        let grandchild = try await Self.pid(writtenTo: file)
        #expect(kill(grandchild, 0) == 0)
        let started = ContinuousClock.now
        run.cancel()
        let result = try await run.value
        #expect(result.exitCode == SIGTERM)    // the shell, ended by the group's SIGTERM
        #expect(ContinuousClock.now - started < .seconds(2))
        #expect(await Self.isGone(grandchild))
    }

    @Test func aTimedOutRunStopsWhatTheChildLeftBehind() async throws {
        let file = NSTemporaryDirectory() + "keryx-grandchild-\(UUID().uuidString)"
        defer { try? FileManager.default.removeItem(atPath: file) }
        await #expect(throws: RunnerError.timedOut) {
            _ = try await runner.run(.init(executable: "/bin/sh",
                arguments: ["-c", "(sleep 30 & echo $! > \"$0\"); echo hi", file],
                timeout: .milliseconds(300)))
        }
        let grandchild = try await Self.pid(writtenTo: file)
        #expect(await Self.isGone(grandchild))
    }

    @Test func aGroupThatIgnoresSigtermIsKilled() async throws {
        let file = NSTemporaryDirectory() + "keryx-grandchild-\(UUID().uuidString)"
        defer { try? FileManager.default.removeItem(atPath: file) }
        let runner = ProcessRunner(grace: .milliseconds(300))
        let started = ContinuousClock.now
        await #expect(throws: RunnerError.timedOut) {
            _ = try await runner.run(.init(executable: "/bin/sh",
                arguments: ["-c", "trap '' TERM; sleep 30 & echo $! > \"$0\"; wait", file],
                timeout: .milliseconds(200)))
        }
        #expect(ContinuousClock.now - started < .seconds(3))
        let grandchild = try await Self.pid(writtenTo: file)
        #expect(await Self.isGone(grandchild))
    }

    @Test func stoppingAStreamRunsOnToTheChildsLastWordsThenEndsReaped() async throws {
        let stream = runner.lines(.init(executable: "/bin/sh", arguments: ["-c", """
            trap 'echo stopping; exit 130' TERM
            echo $$
            while :; do sleep 0.05; done
            """], timeout: nil))
        var lines: [String] = []
        let started = ContinuousClock.now
        for try await line in stream {
            lines.append(line)
            if lines.count == 1 { stream.stop() }
        }
        #expect(ContinuousClock.now - started < .seconds(3))
        #expect(lines.count == 2)
        #expect(lines.last == "stopping")
        // The stream ended only once the child had been reaped: its pid is free at once.
        let child = try #require(pid_t(lines[0]))
        #expect(kill(child, 0) == -1 && errno == ESRCH)
    }

    @Test func stoppingAStreamStopsTheGrandchildToo() async throws {
        let stream = runner.lines(.init(executable: "/bin/sh",
            arguments: ["-c", "sleep 30 & echo $!; wait"], timeout: nil))
        var grandchild: pid_t?
        for try await line in stream {
            grandchild = pid_t(line)
            stream.stop()
        }
        let pid = try #require(grandchild)
        #expect(await Self.isGone(pid))
    }

    @Test func abandoningAStreamStopsTheGrandchildToo() async throws {
        let file = NSTemporaryDirectory() + "keryx-grandchild-\(UUID().uuidString)"
        defer { try? FileManager.default.removeItem(atPath: file) }
        let task = Task {
            for try await _ in runner.lines(.init(executable: "/bin/sh",
                arguments: ["-c", "sleep 30 & echo $! > \"$0\"; wait", file], timeout: nil)) {}
        }
        let grandchild = try await Self.pid(writtenTo: file)
        task.cancel()
        _ = await task.result
        #expect(await Self.isGone(grandchild))
    }

    /// A grandchild that ignores SIGTERM (as a Python mid-write might, for a moment) and
    /// holds nothing open; the shell above it exits at SIGTERM at once. It says its pid only
    /// once the trap is set, so the test cannot stop it before it is stubborn.
    static let stubbornGrandchild =
        #"sh -c 'trap "" TERM; echo $$ > "$1"; exec sleep 30' grandchild "$0" & wait"#

    @Test func aCancelledRunEndsOnlyOnceAGrandchildIgnoringSigtermIsGone() async throws {
        let file = NSTemporaryDirectory() + "keryx-stubborn-\(UUID().uuidString)"
        defer { try? FileManager.default.removeItem(atPath: file) }
        let runner = ProcessRunner(grace: .milliseconds(500))
        let run = Task {
            try await runner.run(.init(executable: "/bin/sh",
                arguments: ["-c", Self.stubbornGrandchild, file], timeout: .seconds(30)))
        }
        let grandchild = try await Self.pid(writtenTo: file)
        let started = ContinuousClock.now
        run.cancel()
        _ = try await run.value
        let took = ContinuousClock.now - started
        // Not at the shell's exit, but after SIGKILL reached the grandchild, and within the bound.
        #expect(took >= .milliseconds(500))
        #expect(took < .seconds(2))
        #expect(kill(grandchild, 0) == -1 && errno == ESRCH)
    }

    @Test func aStoppedStreamEndsOnlyOnceAGrandchildIgnoringSigtermIsGone() async throws {
        let runner = ProcessRunner(grace: .milliseconds(500))
        let stream = runner.lines(.init(executable: "/bin/sh",
            arguments: ["-c", #"sh -c 'trap "" TERM; echo $$; exec sleep 30' & wait"#], timeout: nil))
        var grandchild: pid_t?
        var stopped: ContinuousClock.Instant?
        for try await line in stream {
            grandchild = pid_t(line)
            stopped = ContinuousClock.now
            stream.stop()
        }
        let took = ContinuousClock.now - (try #require(stopped))
        #expect(took >= .milliseconds(500))
        #expect(took < .seconds(2))
        let pid = try #require(grandchild)
        #expect(kill(pid, 0) == -1 && errno == ESRCH)
    }

    @Test func aTimedOutRunEndsOnlyOnceAGrandchildIgnoringSigtermIsGone() async throws {
        let file = NSTemporaryDirectory() + "keryx-stubborn-\(UUID().uuidString)"
        defer { try? FileManager.default.removeItem(atPath: file) }
        let runner = ProcessRunner(grace: .milliseconds(500))
        let started = ContinuousClock.now
        await #expect(throws: RunnerError.timedOut) {
            _ = try await runner.run(.init(executable: "/bin/sh",
                arguments: ["-c", Self.stubbornGrandchild, file], timeout: .milliseconds(200)))
        }
        #expect(ContinuousClock.now - started >= .milliseconds(700))
        let grandchild = try await Self.pid(writtenTo: file)
        #expect(kill(grandchild, 0) == -1 && errno == ESRCH)
    }

    @Test func stillRunningSaysSo() {
        #expect(RunnerError.stillRunning.localizedDescription.contains("still running"))
    }

    @Test func aMissingWorkingDirectoryIsALaunchFailure() async {
        await #expect(throws: RunnerError.self) {
            _ = try await runner.run(.init(executable: "/bin/pwd", workingDirectory: "/nonexistent/keryx"))
        }
    }

    @Test func noStdinReadsAsEmpty() async throws {
        let result = try await runner.run(.init(executable: "/bin/cat", timeout: .seconds(5)))
        #expect(result.exitCode == 0)
        #expect(result.stdout.isEmpty)
    }

    /// The pid a shell wrote to `file`, once it has.
    static func pid(writtenTo file: String) async throws -> pid_t {
        for _ in 0..<500 {
            if let text = try? String(contentsOfFile: file, encoding: .utf8),
               let pid = pid_t(text.trimmingCharacters(in: .whitespacesAndNewlines)) {
                return pid
            }
            try await Task.sleep(for: .milliseconds(10))
        }
        throw CancellationError()
    }

    /// Whether `pid` is gone (`kill -0` fails) within a few seconds: an orphan is reaped by
    /// launchd, not by us, so its pid may linger a moment as a zombie.
    static func isGone(_ pid: pid_t) async -> Bool {
        for _ in 0..<500 {
            if kill(pid, 0) == -1, errno == ESRCH { return true }
            try? await Task.sleep(for: .milliseconds(10))
        }
        return false
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
