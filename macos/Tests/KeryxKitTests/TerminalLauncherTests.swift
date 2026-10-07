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

    @Test func anAllowlistedVariableTheLocationLacksIsUnset() {
        let script = TerminalLauncher.script(for: Self.location, arguments: ["setup"])
        let lines = script.split(separator: "\n").map(String.init)
        for name in ["XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"] {
            #expect(lines.contains("unset \(name)"), "\(name)")
        }
        for name in ["KERYX_HOME", "XDG_DATA_HOME", "PATH", "HOME"] {
            #expect(!lines.contains("unset \(name)"), "\(name)")
        }
        // Only the allowlist is ever unset, and before the command runs.
        let unset = lines.filter { $0.hasPrefix("unset ") && $0 != "unset KERYX_ACTOR JARVIS_ACTOR" }
            .map { String($0.dropFirst("unset ".count)) }
        #expect(Set(unset).isSubset(of: Self.allowlist))
        #expect(lines.lastIndex { $0.hasPrefix("unset ") }! < lines.firstIndex { $0.hasPrefix("exec ") }!)
    }

    @Test func theInstallScriptRunsTheCheckoutsInstallerTheSameWay() throws {
        let script = try #require(TerminalLauncher.installServiceScript(for: Self.location))
        let lines = script.split(separator: "\n").map(String.init)
        #expect(lines[1] == #"rm -f -- "$0""#)
        #expect(lines.contains("unset KERYX_ACTOR JARVIS_ACTOR"))
        #expect(lines.contains("export KERYX_HOME='/Users/x/.config/keryx'"))
        #expect(lines.contains("unset XDG_CONFIG_HOME"))
        #expect(lines.contains("cd '/Users/x/my keryx' || exit 1"))
        #expect(lines.last == "exec '/Users/x/my keryx/scripts/install-launchd.sh'")
        #expect(!script.contains("sk-should-never-appear"))

        var bare = Self.location
        bare.checkout = nil
        #expect(TerminalLauncher.installServiceScript(for: bare) == nil, "no checkout, no installer")
    }

    @Test func withNoCheckoutTheScriptRunsFromTheRoot() {
        var location = Self.location
        location.checkout = nil
        location.executable = "/Users/x/.local/bin/keryx"
        location.workingDirectory = "/Users/x"
        let script = TerminalLauncher.script(for: location, arguments: ["migrate"])
        #expect(script.contains("\ncd '/' || exit 1\n"))
        #expect(!script.contains("cd '/Users/x'"))
    }

    @Test func staleScriptsAreRemovedAndNothingElse() throws {
        let directory = NSTemporaryDirectory() + "keryx-stale-\(UUID().uuidString)"
        try FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(atPath: directory) }
        let now = Date()
        func make(_ name: String, minutesAgo: Double) throws -> String {
            let path = directory + "/" + name
            try Data("#!/bin/sh\n".utf8).write(to: URL(fileURLWithPath: path))
            try FileManager.default.setAttributes(
                [.modificationDate: now.addingTimeInterval(-minutesAgo * 60)], ofItemAtPath: path)
            return path
        }
        let stale = try make("keryx-\(UUID().uuidString).command", minutesAgo: 11)
        let fresh = try make("keryx-\(UUID().uuidString).command", minutesAgo: 2)
        let notOurs = try make("keryx-notes.command", minutesAgo: 60)
        let other = try make("\(UUID().uuidString).command", minutesAgo: 60)

        TerminalLauncher.removeStaleScripts(in: directory, now: now)
        #expect(!FileManager.default.fileExists(atPath: stale))
        #expect(FileManager.default.fileExists(atPath: fresh))
        #expect(FileManager.default.fileExists(atPath: notOurs))
        #expect(FileManager.default.fileExists(atPath: other))
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
