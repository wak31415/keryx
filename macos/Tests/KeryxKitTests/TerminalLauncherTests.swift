import Foundation
import Testing
@testable import KeryxKit

@Suite struct TerminalLauncherTests {
    static let allowlist: Set<String> = [
        "KERYX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
        "PATH", "HOME",
    ]

    @Test func quoteIsPosix() {
        #expect(TerminalLauncher.quote("it's") == #"'it'"'"'s'"#)
        #expect(TerminalLauncher.quote("plain") == "'plain'")
        #expect(TerminalLauncher.quote("") == "''")
        #expect(TerminalLauncher.quote("$HOME `x` \"y\"") == "'$HOME `x` \"y\"'")
    }

    static var location: KeryxLocation {
        KeryxLocation(
            checkout: "/Users/x/my keryx",
            executable: "/Users/x/my keryx/.venv/bin/keryx",
            prefixArguments: [],
            environment: [
                "KERYX_HOME": "/Users/x/.config/keryx",
                "XDG_DATA_HOME": "/Users/x/.local/share",
                "PATH": "/usr/bin:/bin",
                "HOME": "/Users/x",
                "OPENAI_API_KEY": "sk-should-never-appear",
                "KERYX_ACTOR": "service",
                "NO_COLOR": "1",
            ],
            workingDirectory: "/Users/x/my keryx",
            source: .launchAgent)
    }

    @Test func scriptExportsOnlyTheAllowlist() {
        let script = TerminalLauncher.script(for: Self.location, arguments: ["setup"])
        #expect(script.hasPrefix("#!/bin/sh\n"))
        #expect(script.contains("export KERYX_HOME='/Users/x/.config/keryx'"))
        #expect(script.contains("cd '/Users/x/my keryx'"))
        #expect(script.contains("exec '/Users/x/my keryx/.venv/bin/keryx' 'setup'"))
        #expect(!script.contains("sk-should-never-appear"))
        let exported = script.split(separator: "\n")
            .filter { $0.hasPrefix("export ") }
            .map { String($0.dropFirst("export ".count).prefix { $0 != "=" }) }
        #expect(!exported.isEmpty)
        #expect(Set(exported).isSubset(of: Self.allowlist))
    }

    @Test func scriptDeletesItselfFirstAndDropsTheActor() throws {
        let script = TerminalLauncher.script(for: Self.location, arguments: ["auth", "login", "claude"])
        let lines = script.split(separator: "\n").map(String.init)
        #expect(lines[1] == #"rm -f -- "$0""#)
        #expect(lines.contains("unset KERYX_ACTOR JARVIS_ACTOR"))
        #expect(lines.last == "exec '/Users/x/my keryx/.venv/bin/keryx' 'auth' 'login' 'claude'")
    }

    @Test func scriptRunsUvWithItsPrefix() {
        var location = Self.location
        location.executable = "/opt/homebrew/bin/uv"
        location.prefixArguments = ["run", "--project", "/Users/x/my keryx", "--no-sync", "keryx"]
        let script = TerminalLauncher.script(for: location, arguments: ["migrate"])
        #expect(script.contains(
            "exec '/opt/homebrew/bin/uv' 'run' '--project' '/Users/x/my keryx' '--no-sync' 'keryx' 'migrate'"))
    }

    @Test func scriptIsRunnableShell() async throws {
        // The text is valid `sh`: `-n` parses it without running anything.
        let path = NSTemporaryDirectory() + "keryx-test-\(UUID().uuidString).command"
        try TerminalLauncher.script(for: Self.location, arguments: ["it's"]).write(
            toFile: path, atomically: true, encoding: .utf8)
        defer { try? FileManager.default.removeItem(atPath: path) }
        let result = try await ProcessRunner().run(.init(executable: "/bin/sh", arguments: ["-n", path]))
        #expect(result.exitCode == 0)
    }
}
