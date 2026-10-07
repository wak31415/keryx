import Foundation
import Testing
@testable import KeryxKit

@Suite struct LocatorTests {
    static let home = "/Users/x"
    static let plistPath = "/Users/x/Library/LaunchAgents/dev.keryx.agent.plist"
    static let pyproject = Data("""
        [build-system]
        requires = ["hatchling"]

        [project]
        name = "keryx"
        version = "0.1.0"
        """.utf8)

    static func fixture(_ name: String) throws -> Data {
        let url = try #require(Bundle.module.url(
            forResource: name, withExtension: nil, subdirectory: "Fixtures"))
        return try Data(contentsOf: url)
    }

    /// A runner whose login shell prints `env` (NUL-separated) and `command -v keryx`.
    static func shell(env: [String: String]? = nil, keryx: String? = nil) -> FakeRunner {
        FakeRunner { request in
            #expect(request.executable == "/bin/zsh")
            let script = request.arguments.last ?? ""
            if script == "env -0", let env {
                let body = env.map { "\($0.key)=\($0.value)" }.joined(separator: "\0") + "\0"
                return .init(exitCode: 0, stdout: Data(body.utf8), stderr: Data())
            }
            if script == "command -v keryx", let keryx {
                return .init(exitCode: 0, stdout: Data((keryx + "\n").utf8), stderr: Data())
            }
            return .init(exitCode: 1, stdout: Data(), stderr: Data())
        }
    }

    // MARK: - The launch agent

    @Test func plistWithWorkingDirectory() async throws {
        var fs = FakeFileSystem()
        fs.files[Self.plistPath] = try Self.fixture("agent.plist")
        fs.files["/Users/x/code/keryx/pyproject.toml"] = Self.pyproject
        fs.executables = ["/Users/x/.local/bin/uv"]
        let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)

        let location = try await locator.locate(preference: nil).get()
        #expect(location.source == .launchAgent)
        #expect(location.checkout == "/Users/x/code/keryx")
        #expect(location.executable == "/Users/x/.local/bin/uv")
        #expect(location.prefixArguments == ["run", "--project", "/Users/x/code/keryx", "--no-sync", "keryx"])
        #expect(location.workingDirectory == "/Users/x/code/keryx")
        #expect(location.environment["KERYX_HOME"] == "/Users/x/.config/keryx")
        #expect(location.environment["XDG_STATE_HOME"] == "/Users/x/.local/state")
        #expect(location.environment["HOME"] == "/Users/x")
        #expect(location.environment["USER"] != nil)
        #expect(location.environment["NO_COLOR"] == "1")
    }

    @Test func plistWithoutWorkingDirectoryUsesProject() async throws {
        var fs = FakeFileSystem()
        fs.files[Self.plistPath] = try Self.fixture("agent-no-workdir.plist")
        fs.files["/Users/x/src/keryx-checkout/pyproject.toml"] = Self.pyproject
        fs.executables = ["/opt/homebrew/bin/uv"]
        let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)

        let location = try await locator.locate(preference: nil).get()
        #expect(location.checkout == "/Users/x/src/keryx-checkout")
        #expect(location.executable == "/opt/homebrew/bin/uv")
        #expect(location.workingDirectory == "/Users/x/src/keryx-checkout")
        // HOME comes from the minimal base when the plist does not set it.
        #expect(location.environment["HOME"] == "/Users/x")
        #expect(location.environment["PATH"] == "/opt/homebrew/bin:/usr/bin:/bin")
    }

    @Test func venvKeryxIsPreferredOverUv() async throws {
        var fs = FakeFileSystem()
        fs.files[Self.plistPath] = try Self.fixture("agent.plist")
        fs.files["/Users/x/code/keryx/pyproject.toml"] = Self.pyproject
        fs.executables = ["/Users/x/.local/bin/uv", "/Users/x/code/keryx/.venv/bin/keryx"]
        let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)

        let location = try await locator.locate(preference: nil).get()
        #expect(location.executable == "/Users/x/code/keryx/.venv/bin/keryx")
        #expect(location.prefixArguments == [])
    }

    @Test func plistRunningKeryxDirectlyNeedsNoCheckout() async throws {
        let plist: [String: Any] = [
            "Label": "dev.keryx.agent",
            "ProgramArguments": ["/Users/x/.local/bin/keryx", "serve"],
            "EnvironmentVariables": ["KERYX_HOME": "/Users/x/.config/keryx"],
        ]
        var fs = FakeFileSystem()
        fs.files[Self.plistPath] = try PropertyListSerialization.data(
            fromPropertyList: plist, format: .xml, options: 0)
        fs.executables = ["/Users/x/.local/bin/keryx"]
        let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)

        let location = try await locator.locate(preference: nil).get()
        #expect(location.source == .launchAgent)
        #expect(location.executable == "/Users/x/.local/bin/keryx")
        #expect(location.prefixArguments == [])
        #expect(location.checkout == nil)
        #expect(location.workingDirectory == "/Users/x")
        #expect(location.environment["KERYX_HOME"] == "/Users/x/.config/keryx")
    }

    @Test func plistWhoseCheckoutIsNotKeryxFallsThrough() async throws {
        var fs = FakeFileSystem()
        fs.files[Self.plistPath] = try Self.fixture("agent.plist")
        fs.files["/Users/x/code/keryx/pyproject.toml"] = Data("[project]\nname = \"other\"\n".utf8)
        fs.executables = ["/Users/x/.local/bin/uv"]
        let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)

        let failure = try #require(await locator.locate(preference: nil).failure)
        #expect(failure.lookedAt.contains { $0.contains("/code/keryx") && $0.contains("pyproject.toml") })
    }

    // MARK: - The preference

    @Test func preferenceFolderWins() async throws {
        var fs = FakeFileSystem()
        fs.files["/Users/x/elsewhere/pyproject.toml"] = Self.pyproject
        fs.files[Self.plistPath] = try Self.fixture("agent.plist")
        fs.executables = ["/Users/x/elsewhere/.venv/bin/keryx"]
        let runner = Self.shell(env: ["PATH": "/login/bin", "KERYX_ACTOR": "service", "HOME": "/Users/x"])
        let locator = KeryxLocator(fileSystem: fs, runner: runner, home: Self.home)

        let location = try await locator.locate(preference: "/Users/x/elsewhere").get()
        #expect(location.source == .preference)
        #expect(location.checkout == "/Users/x/elsewhere")
        #expect(location.executable == "/Users/x/elsewhere/.venv/bin/keryx")
        #expect(location.environment["PATH"] == "/login/bin")
        #expect(location.environment["KERYX_ACTOR"] == nil)
        let envCall = try #require(runner.requests.first)
        #expect(envCall.arguments == ["-lic", "env -0"])
        #expect(envCall.timeout == .seconds(5))
    }

    @Test func preferenceWithoutPyprojectIsRejected() async throws {
        var fs = FakeFileSystem()
        fs.files[Self.plistPath] = try Self.fixture("agent.plist")
        fs.files["/Users/x/code/keryx/pyproject.toml"] = Self.pyproject
        fs.executables = ["/Users/x/.local/bin/uv"]
        let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)

        let result = await locator.locate(preference: "/Users/x/Desktop")
        // Rejected, so the launch agent is used — and the failed step is not silent.
        let location = try result.get()
        #expect(location.source == .launchAgent)

        // With nothing behind it, the sentence is in the failure.
        let empty = KeryxLocator(fileSystem: FakeFileSystem(), runner: Self.shell(), home: Self.home)
        let failure = try #require(await empty.locate(preference: "/Users/x/Desktop").failure)
        #expect(failure.lookedAt.first?.contains("~/Desktop") == true)
        #expect(failure.lookedAt.first?.contains("pyproject.toml") == true)
    }

    // MARK: - The login shell

    @Test func loginShellFindsAVenvKeryx() async throws {
        var fs = FakeFileSystem()
        fs.executables = ["/Users/x/code/keryx/.venv/bin/keryx"]
        let runner = Self.shell(env: ["PATH": "/a:/b", "HOME": "/Users/x"],
                                keryx: "/Users/x/code/keryx/.venv/bin/keryx")
        let locator = KeryxLocator(fileSystem: fs, runner: runner, home: Self.home)

        let location = try await locator.locate(preference: nil).get()
        #expect(location.source == .loginShell)
        #expect(location.checkout == "/Users/x/code/keryx")
        #expect(location.executable == "/Users/x/code/keryx/.venv/bin/keryx")
        #expect(location.workingDirectory == "/Users/x/code/keryx")
        #expect(location.environment["PATH"] == "/a:/b")
    }

    @Test func loginShellFindsABareKeryx() async throws {
        var fs = FakeFileSystem()
        fs.executables = ["/opt/homebrew/bin/keryx"]
        let runner = Self.shell(env: ["PATH": "/a"], keryx: "/opt/homebrew/bin/keryx")
        let locator = KeryxLocator(fileSystem: fs, runner: runner, home: Self.home)

        let location = try await locator.locate(preference: nil).get()
        #expect(location.checkout == nil)
        #expect(location.executable == "/opt/homebrew/bin/keryx")
        #expect(location.workingDirectory == "/Users/x")
    }

    @Test func loginShellAliasIsNotAPath() async throws {
        let runner = Self.shell(env: ["PATH": "/a"], keryx: "alias keryx='uv run keryx'")
        let locator = KeryxLocator(fileSystem: FakeFileSystem(), runner: runner, home: Self.home)
        #expect(await locator.locate(preference: nil).failure != nil)
    }

    @Test func loginShellNoiseBeforeEnvIsIgnored() async throws {
        let runner = FakeRunner { request in
            if request.arguments.last == "env -0" {
                return .init(exitCode: 0, stdout: Data("Welcome!\nPATH=/p\0HOME=/Users/x\0".utf8),
                             stderr: Data())
            }
            return .init(exitCode: 0, stdout: Data("noise\n/u/bin/keryx\n".utf8), stderr: Data())
        }
        var fs = FakeFileSystem()
        fs.executables = ["/u/bin/keryx"]
        let locator = KeryxLocator(fileSystem: fs, runner: runner, home: Self.home)

        let location = try await locator.locate(preference: nil).get()
        #expect(location.environment["PATH"] == "/p")
        #expect(location.executable == "/u/bin/keryx")
    }

    @Test func uvToolInstallIsTheLastResort() async throws {
        var fs = FakeFileSystem()
        fs.executables = ["/Users/x/.local/bin/keryx"]
        let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)

        let location = try await locator.locate(preference: nil).get()
        #expect(location.executable == "/Users/x/.local/bin/keryx")
        #expect(location.checkout == nil)
        #expect(location.prefixArguments == [])
    }

    @Test func nothingFoundListsAllThreePlaces() async throws {
        let locator = KeryxLocator(fileSystem: FakeFileSystem(), runner: Self.shell(), home: Self.home)
        let failure = try #require(await locator.locate(preference: nil).failure)
        #expect(failure.lookedAt.count >= 3)
        #expect(failure.lookedAt.contains { $0.contains("folder") })
        #expect(failure.lookedAt.contains { $0.contains("dev.keryx.agent.plist") })
        #expect(failure.lookedAt.contains { $0.contains("login shell") })
    }

    // MARK: - KERYX_APP_ENVIRONMENT=inherit

    /// A runner that fails the test if anything is run: `inherit` asks no login shell.
    static func noShell() -> FakeRunner {
        FakeRunner { request in
            Issue.record("ran \(request.executable) \(request.arguments)")
            return .init(exitCode: 1, stdout: Data(), stderr: Data())
        }
    }

    @Test func inheritTakesTheAppsOwnEnvironmentForAChosenFolder() async throws {
        var fs = FakeFileSystem()
        fs.files["/sb/keryx/pyproject.toml"] = Self.pyproject
        fs.executables = ["/sb/keryx/.venv/bin/keryx"]
        let runner = Self.noShell()
        let locator = KeryxLocator(
            fileSystem: fs, runner: runner, home: Self.home,
            environment: ["KERYX_APP_ENVIRONMENT": "inherit", "HOME": "/sb/home", "PATH": "/sb/bin",
                          "KERYX_HOME": "/sb/home/.config/keryx", "KERYX_ACTOR": "service"])

        let location = try await locator.locate(preference: "/sb/keryx").get()
        #expect(location.source == .preference)
        #expect(location.executable == "/sb/keryx/.venv/bin/keryx")
        #expect(location.environment["KERYX_HOME"] == "/sb/home/.config/keryx")
        #expect(location.environment["HOME"] == "/sb/home")
        #expect(location.environment["PATH"] == "/sb/bin")
        #expect(location.environment["KERYX_ACTOR"] == nil)
        #expect(runner.requests.isEmpty)
    }

    @Test func inheritFindsKeryxOnTheAppsOwnPath() async throws {
        var fs = FakeFileSystem()
        fs.executables = ["/sb/bin/keryx"]
        let runner = Self.noShell()
        let locator = KeryxLocator(
            fileSystem: fs, runner: runner, home: Self.home,
            environment: ["KERYX_APP_ENVIRONMENT": "inherit", "PATH": "/nowhere:/sb/bin"])

        let location = try await locator.locate(preference: nil).get()
        #expect(location.source == .loginShell)
        #expect(location.executable == "/sb/bin/keryx")
        #expect(location.environment["PATH"] == "/nowhere:/sb/bin")
        #expect(location.environment["HOME"] == "/Users/x")
        #expect(runner.requests.isEmpty)
    }

    @Test func anyOtherValueStillAsksTheLoginShell() async throws {
        var fs = FakeFileSystem()
        fs.files["/Users/x/elsewhere/pyproject.toml"] = Self.pyproject
        fs.executables = ["/Users/x/elsewhere/.venv/bin/keryx"]
        let runner = Self.shell(env: ["PATH": "/login/bin", "HOME": "/Users/x"])
        let locator = KeryxLocator(
            fileSystem: fs, runner: runner, home: Self.home,
            environment: ["KERYX_APP_ENVIRONMENT": "yes", "PATH": "/app/bin"])

        let location = try await locator.locate(preference: "/Users/x/elsewhere").get()
        #expect(location.environment["PATH"] == "/login/bin")
        #expect(runner.requests.first?.arguments == ["-lic", "env -0"])
    }

    // MARK: - Sanitise

    @Test func actorVariablesAreStripped() {
        #expect(Sanitise.environment(["KERYX_ACTOR": "service", "JARVIS_ACTOR": "service", "PATH": "/bin"])
            == ["PATH": "/bin", "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"])
    }
}

extension Result {
    var failure: Failure? {
        if case let .failure(error) = self { return error }
        return nil
    }
}
