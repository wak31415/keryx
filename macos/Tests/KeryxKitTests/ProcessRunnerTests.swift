import Foundation
import Testing
@testable import KeryxKit

/// A class, so `deinit` can stop whatever a test started, whichever assertion failed: every
/// runner here hands its children to `leftovers`. Waits are generous ceilings (10 s) that
/// return as soon as their condition holds.
@Suite final class ProcessRunnerTests: Sendable {
    let leftovers = Leftovers()
    let runner: ProcessRunner

    init() {
        runner = ProcessRunner(observingChild: leftovers.add)
    }

    deinit {
        leftovers.clean()
    }

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
                arguments: ["-c", "(sleep 30 &) ; echo hi"], timeout: .milliseconds(300)))
        }
        #expect(ContinuousClock.now - started < .seconds(10))
    }

    @Test func shortRunWithNoOutput() async throws {
        let result = try await runner.run(.init(executable: "/usr/bin/true", timeout: .seconds(5)))
        #expect(result.exitCode == 0)
        #expect(result.stdout.isEmpty)
        #expect(result.stderr.isEmpty)
    }

    // MARK: - Detached (Quit's `service stop --all`)

    /// `CommandRequest.detached`'s whole reason to exist: stdout and stderr are a file
    /// each, never a pipe — checked from the child's own side (`stat -f %HT`, which a
    /// pipe reports as "Fifo", a file as "Regular File"), so this is also a check that
    /// nothing upstream still hands it a pipe by mistake.
    @Test func aDetachedRunsOutputIsARegularFileNotAPipe() async throws {
        let result = try await runner.run(.init(executable: "/bin/sh",
            arguments: ["-c", "stat -f %HT /dev/fd/1; stat -f %HT /dev/fd/2 >&2"], detached: true))
        #expect(result.stdoutText.trimmingCharacters(in: .whitespacesAndNewlines) == "Regular File")
        #expect(result.stderrText.trimmingCharacters(in: .whitespacesAndNewlines) == "Regular File")
    }

    /// The bug this round fixes: a pipe's reader going away (the app quitting) leaves a
    /// child that writes on after it — a log line, the final JSON reply — meeting a
    /// closed pipe: SIGPIPE, or in Python's case `BrokenPipeError` on the very next
    /// write. A file has no reader to lose. This never abandons the read the way Quit
    /// does (that is `AppModel`'s `raced`, tested there); it proves the mechanism a
    /// detached run actually uses — a file, read once the child has exited — copes with
    /// more output than a pipe's buffer (64 KiB) and with a gap in the middle, neither of
    /// which a live pipe reader would even be there for once the app had gone.
    @Test func aDetachedChildThatKeepsWritingPastWhenAPipeReaderWouldHaveGoneStillCompletes() async throws {
        let result = try await runner.run(.init(executable: "/bin/sh", arguments: ["-c",
            "head -c 100000 /dev/zero | tr '\\0' a; sleep 0.3; head -c 100000 /dev/zero | tr '\\0' b; echo done"],
            timeout: .seconds(10), detached: true))
        #expect(result.exitCode == 0)
        #expect(result.stdout.count == 200_005)
        #expect(result.stdoutText.hasPrefix(String(repeating: "a", count: 100_000)))
        #expect(result.stdoutText.contains(String(repeating: "b", count: 100_000)))
        #expect(result.stdoutText.hasSuffix("done\n"))
    }

    /// `request.timeout` exists to kill a run taking too long; a detached run must never
    /// be killed by the app that spawned it, so this timer is not armed for one — a
    /// timeout shorter than the child's own sleep would otherwise throw `.timedOut`
    /// (`timesOut()`, above), well before the child is done.
    @Test func aDetachedRunIsNeverBoundedByItsOwnTimeout() async throws {
        let result = try await runner.run(.init(executable: "/bin/sh",
            arguments: ["-c", "sleep 0.3; echo done"], timeout: .milliseconds(100), detached: true))
        #expect(result.exitCode == 0)
        #expect(result.stdoutText == "done\n")
    }

    /// The output files are the app's own, under its temporary directory, and are
    /// removed once read — nothing of a detached run's is left behind once it is done.
    /// Checks exactly the two paths this run's own `ProcessRunner` made (the test hook,
    /// same as `observingChild`), not the temporary directory's whole contents: this
    /// suite's other `detached` tests share it and run concurrently, so a directory-wide
    /// snapshot races.
    @Test func aDetachedRunsOutputFilesAreRemovedOnceRead() async throws {
        let made = Holder<(stdout: URL, stderr: URL)>()
        let runner = ProcessRunner(observingChild: leftovers.add, observingOutputFiles: made.set)
        let result = try await runner.run(.init(executable: "/bin/sh", arguments: ["-c", "echo hi"],
                                                 detached: true))
        #expect(result.stdoutText == "hi\n")
        let files = try #require(made.value)
        #expect(!FileManager.default.fileExists(atPath: files.stdout.path))
        #expect(!FileManager.default.fileExists(atPath: files.stderr.path))
    }

    @Test func aFinishedRunIsFreed() async throws {
        let probe = WeakProbe()
        let runner = ProcessRunner(observingCollector: { probe.object = $0 }, observingChild: leftovers.add)
        let result = try await runner.run(.init(executable: "/bin/sh",
            arguments: ["-c", "echo hi; echo err >&2"], timeout: .seconds(5)))
        #expect(result.stdoutText == "hi\n")
        #expect(probe.wasObserved)
        #expect(await probe.isFreed())
    }

    @Test func aTimedOutRunIsFreed() async throws {
        let probe = WeakProbe()
        let runner = ProcessRunner(observingCollector: { probe.object = $0 }, observingChild: leftovers.add)
        await #expect(throws: RunnerError.timedOut) {
            _ = try await runner.run(.init(executable: "/bin/sh",
                arguments: ["-c", "(sleep 30 &) ; echo hi"], timeout: .milliseconds(200)))
        }
        #expect(probe.wasObserved)
        #expect(await probe.isFreed())
    }

    @Test func aLaunchFailureIsFreed() async throws {
        let probe = WeakProbe()
        let runner = ProcessRunner(observingCollector: { probe.object = $0 }, observingChild: leftovers.add)
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

    @Test func aStreamThatMergesStderrReadsItInOrder() async throws {
        var got: [String] = []
        for try await line in runner.lines(.init(executable: "/bin/sh",
            arguments: ["-c", "echo '==> Fetching'; echo 'Error: no bottle' >&2; echo after"], timeout: nil,
            mergesStderr: true)) { got.append(line) }
        #expect(got == ["==> Fetching", "Error: no bottle", "after"])
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
        #expect(ContinuousClock.now - started < .seconds(10))
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
        let runner = ProcessRunner(grace: .milliseconds(300), observingChild: leftovers.add)
        let started = ContinuousClock.now
        await #expect(throws: RunnerError.timedOut) {
            _ = try await runner.run(.init(executable: "/bin/sh",
                arguments: ["-c", "trap '' TERM; sleep 30 & echo $! > \"$0\"; wait", file],
                timeout: .milliseconds(200)))
        }
        #expect(ContinuousClock.now - started < .seconds(10))
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
        #expect(ContinuousClock.now - started < .seconds(10))
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
        let runner = ProcessRunner(grace: .milliseconds(500), observingChild: leftovers.add)
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
        #expect(took < .seconds(10))
        #expect(kill(grandchild, 0) == -1 && errno == ESRCH)
    }

    @Test func aStoppedStreamEndsOnlyOnceAGrandchildIgnoringSigtermIsGone() async throws {
        let runner = ProcessRunner(grace: .milliseconds(500), observingChild: leftovers.add)
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
        #expect(took < .seconds(10))
        let pid = try #require(grandchild)
        #expect(kill(pid, 0) == -1 && errno == ESRCH)
    }

    @Test func aTimedOutRunEndsOnlyOnceAGrandchildIgnoringSigtermIsGone() async throws {
        let file = NSTemporaryDirectory() + "keryx-stubborn-\(UUID().uuidString)"
        defer { try? FileManager.default.removeItem(atPath: file) }
        let runner = ProcessRunner(grace: .milliseconds(500), observingChild: leftovers.add)
        let started = ContinuousClock.now
        await #expect(throws: RunnerError.timedOut) {
            _ = try await runner.run(.init(executable: "/bin/sh",
                arguments: ["-c", Self.stubbornGrandchild, file], timeout: .milliseconds(200)))
        }
        #expect(ContinuousClock.now - started >= .milliseconds(700))
        let grandchild = try await Self.pid(writtenTo: file)
        #expect(kill(grandchild, 0) == -1 && errno == ESRCH)
    }

    // MARK: - The group is signalled only while the leader holds its id

    @Test func aStoppedLeaderIsReapedOnceAndLastAndNothingIsSignalledAfter() async throws {
        let file = NSTemporaryDirectory() + "keryx-stubborn-\(UUID().uuidString)"
        defer { try? FileManager.default.removeItem(atPath: file) }
        let children = Leftovers()
        let runner = ProcessRunner(grace: .milliseconds(300), observingChild: { [leftovers] in
            leftovers.add($0)
            children.add($0)
        })
        let run = Task {
            try await runner.run(.init(executable: "/bin/sh",
                arguments: ["-c", Self.stubbornGrandchild, file], timeout: .seconds(30)))
        }
        let grandchild = try await Self.pid(writtenTo: file)
        run.cancel()
        _ = try await run.value
        let child = try #require(children.all.first)

        // Done only once the grandchild is gone, and the leader reaped, once, after the signals.
        #expect(kill(grandchild, 0) == -1 && errno == ESRCH)
        #expect(child.events == [.signalled(SIGTERM), .signalled(SIGKILL), .reaped])
        #expect(waitpid(child.pid, nil, WNOHANG) == -1 && errno == ECHILD)

        // Nothing more, ever: not when the timers would have fired, not on a second stop.
        try await Task.sleep(for: .milliseconds(300) + Child.killWait + .milliseconds(200))
        child.stop()
        #expect(child.events == [.signalled(SIGTERM), .signalled(SIGKILL), .reaped])
    }

    @Test func aRunThatEndsByItselfIsReapedAndItsGroupNeverSignalled() async throws {
        let children = Leftovers()
        let runner = ProcessRunner(observingChild: { children.add($0) })
        let result = try await runner.run(.init(executable: "/bin/sh", arguments: ["-c", "echo hi"],
                                                timeout: .seconds(10)))
        #expect(result.stdoutText == "hi\n")
        let child = try #require(children.all.first)
        #expect(child.events == [.reaped])
        #expect(waitpid(child.pid, nil, WNOHANG) == -1 && errno == ECHILD)
        child.stop()   // after completion: closed, so not signalled
        #expect(child.events == [.reaped])
    }

    @Test func aStreamThatEndsByItselfIsNeverSignalledEvenStoppedAfter() async throws {
        let children = Leftovers()
        let runner = ProcessRunner(observingChild: { children.add($0) })
        let stream = runner.lines(.init(executable: "/bin/sh", arguments: ["-c", "echo a"], timeout: nil))
        var lines: [String] = []
        for try await line in stream { lines.append(line) }
        #expect(lines == ["a"])
        stream.stop()
        let child = try #require(children.all.first)
        #expect(child.events == [.reaped])
    }

    @Test func aGrandchildThatHoldsThePipesIsStoppedAtTheDeadlineWhileTheLeaderIsAZombie() async throws {
        // The shell exits at once, its background sleep holding stdout: the leader is kept a
        // zombie, not reaped, so the deadline can still signal the group.
        let children = Leftovers()
        let runner = ProcessRunner(observingChild: { [leftovers] in
            leftovers.add($0)
            children.add($0)
        })
        await #expect(throws: RunnerError.timedOut) {
            _ = try await runner.run(.init(executable: "/bin/sh",
                arguments: ["-c", "sleep 30 & echo $!"], timeout: .milliseconds(300)))
        }
        let child = try #require(children.all.first)
        #expect(child.events == [.signalled(SIGTERM), .reaped])
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

    // MARK: - Input while it runs

    @Test func aLineSentReachesTheChildWhileItRuns() async throws {
        // It asks, reads one line, says what it read, and ends.
        let stream = runner.interactiveLines(.init(executable: "/bin/sh", arguments: ["-c", """
            echo ask
            read answer
            echo "got $answer"
            """], timeout: nil))
        #expect(stream.acceptsInput)
        var lines: [String] = []
        for try await line in stream {
            lines.append(line)
            if line == "ask" { #expect(stream.send("c#s 42")) }
        }
        #expect(lines == ["ask", "got c#s 42"])
        // The child has exited: its input is closed, and nothing more is taken.
        #expect(!stream.send("late"))
    }

    @Test func startingInputIsWrittenFirstAndStdinStaysOpen() async throws {
        let stream = runner.interactiveLines(.init(executable: "/bin/sh", arguments: ["-c", """
            read first; echo "first $first"
            read second; echo "second $second"
            """], stdin: Data("one\n".utf8), timeout: nil))
        var lines: [String] = []
        for try await line in stream {
            lines.append(line)
            if lines.count == 1 { stream.send("two") }
        }
        #expect(lines == ["first one", "second two"])
    }

    @Test func closingTheInputIsTheEndOfIt() async throws {
        let stream = runner.interactiveLines(.init(executable: "/bin/sh", arguments: ["-c", """
            echo ready
            cat
            echo eof
            """], timeout: nil))
        var lines: [String] = []
        for try await line in stream {
            lines.append(line)
            if line == "ready" {
                stream.send("a")
                stream.closeInput()
                #expect(!stream.send("after"))
            }
        }
        #expect(lines == ["ready", "a", "eof"])
    }

    @Test func aLineWithABreakInItIsNotSent() async throws {
        let stream = runner.interactiveLines(.init(executable: "/bin/sh", arguments: ["-c", """
            echo ready
            read one; echo "got $one"
            """], timeout: nil))
        for try await line in stream where line == "ready" {
            #expect(!stream.send("two\nlines"))
            #expect(!stream.send("cr\r"))
            #expect(stream.send("one"))
        }
    }

    @Test func stoppingAnInteractiveStreamStopsAChildWaitingOnItsInput() async throws {
        let stream = runner.interactiveLines(.init(executable: "/bin/sh", arguments: ["-c", """
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
        #expect(ContinuousClock.now - started < .seconds(10))
        #expect(lines.last == "stopping")
        #expect(!stream.send("after"))
        let child = try #require(pid_t(lines[0]))
        #expect(kill(child, 0) == -1 && errno == ESRCH)
    }

    @Test func aChildThatClosedItsStdinIsSentToSafely() async throws {
        // It closes its stdin, says so, then waits: writing to it is EPIPE, never SIGPIPE.
        let stream = runner.interactiveLines(.init(executable: "/bin/sh", arguments: ["-c", """
            exec 0<&-
            echo closed
            read -t 1 nothing 2>/dev/null
            sleep 0.3
            echo still here
            """], timeout: nil))
        var lines: [String] = []
        for try await line in stream {
            lines.append(line)
            if line == "closed" {
                // Queued; the write fails in the pipe, and the app goes on.
                for _ in 0..<50 { stream.send(String(repeating: "x", count: 4096)) }
            }
        }
        #expect(lines == ["closed", "still here"])
    }

    @Test func sendsRacingACloseNeverLandBehindIt() async throws {
        // Many writers and several closers at once: nothing is queued behind the close, so
        // no write can reach the descriptor's number once something else has it.
        for _ in 0..<50 {
            var fds: [Int32] = [-1, -1]
            #expect(pipe(&fds) == 0)
            let reader = fds[0]
            _ = fcntl(reader, F_SETFL, O_NONBLOCK)
            let writer = InputWriter(fd: fds[1])
            await withTaskGroup(of: Void.self) { group in
                for index in 0..<16 {
                    group.addTask {
                        for line in 0..<20 { _ = writer.write(Data("\(index)-\(line)\n".utf8)) }
                    }
                }
                for _ in 0..<3 { group.addTask { writer.close() } }
            }
            #expect(writer.lateWrites == 0)
            #expect(!writer.write(Data("after\n".utf8)))
            // Its number may be reused now: a pipe that takes it hears nothing from us.
            var reused: [Int32] = [-1, -1]
            #expect(pipe(&reused) == 0)
            _ = fcntl(reused[0], F_SETFL, O_NONBLOCK)
            #expect(writer.lateWrites == 0)
            var byte: UInt8 = 0
            #expect(Darwin.read(reused[0], &byte, 1) == -1 && errno == EAGAIN)
            // What was written before the close arrived whole, line by line.
            var bytes = [UInt8](repeating: 0, count: 65_536)
            let count = Darwin.read(reader, &bytes, bytes.count)
            if count > 0 {
                let text = String(decoding: bytes[0..<count], as: UTF8.self)
                #expect(text.hasSuffix("\n"))
                #expect(text.split(separator: "\n").allSatisfy { $0.contains("-") })
            }
            for fd in [reader, reused[0], reused[1]] { close(fd) }
        }
    }

    @Test func aPlainStreamTakesNoInput() async throws {
        let stream = runner.lines(.init(executable: "/bin/sh", arguments: ["-c", "echo a"], timeout: nil))
        #expect(!stream.acceptsInput)
        #expect(!stream.send("x"))
        for try await _ in stream {}
    }

    /// The pid a shell wrote to `file`, once it has.
    static func pid(writtenTo file: String) async throws -> pid_t {
        for _ in 0..<1000 {
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
        for _ in 0..<1000 {
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

/// One value, set at most once, from wherever a test hook calls back.
private final class Holder<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var _value: Value?

    var value: Value? { lock.lock(); defer { lock.unlock() }; return _value }

    func set(_ value: Value) { lock.lock(); _value = value; lock.unlock() }
}

/// The children a test's runners started, so the suite's `deinit` can stop whatever is
/// still going (`Child.stop()`, which signals a group only while it is still the child's).
final class Leftovers: @unchecked Sendable {
    private let lock = NSLock()
    private var children: [Child] = []

    var all: [Child] { lock.withLock { children } }

    func add(_ child: Child) { lock.withLock { children.append(child) } }

    func clean() { all.forEach { $0.stop() } }
}
