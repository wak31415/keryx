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
