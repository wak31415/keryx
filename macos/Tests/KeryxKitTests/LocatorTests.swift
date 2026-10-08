import Foundation
import Testing
@testable import KeryxKit
import KeryxTestSupport
import KeryxPreview

@Suite struct LocatorTests {
    static let home = "/Users/x"
    static let plistPath = "/Users/x/Library/LaunchAgents/dev.keryx.agent.plist"
    // The real shape: the distribution is `keryx-voice`, with `keryx` as its console script.
    static let pyproject = Data("""
        [build-system]
        requires = ["hatchling"]

        [project]
        name = "keryx-voice"
        version = "0.1.0"

        [project.scripts]
        keryx = "keryx.cli:app"
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
            if script == LoginShell.script, let env {
                return .init(exitCode: 0, stdout: envOutput(env), stderr: Data())
            }
            if script == "command -v keryx", let keryx {
                return .init(exitCode: 0, stdout: Data((keryx + "\n").utf8), stderr: Data())
            }
            return .init(exitCode: 1, stdout: Data(), stderr: Data())
        }
    }

    /// What the login shell prints for `env`: the marker between NULs, then the entries.
    static func envOutput(_ env: [String: String], before: String = "") -> Data {
        let body = env.map { "\($0.key)=\($0.value)" }.joined(separator: "\0") + "\0"
        return Data((before + "\0" + LoginShell.marker + "\0" + body).utf8)
    }

    static func plist(_ object: [String: Any]) throws -> Data {
        try PropertyListSerialization.data(fromPropertyList: object, format: .xml, options: 0)
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
        // No checkout: from `/`, as launchd runs it, never from HOME.
        #expect(location.workingDirectory == "/")
        #expect(location.runDirectory == "/")
        #expect(location.environment["KERYX_HOME"] == "/Users/x/.config/keryx")
    }

    @Test func aBareKeryxPlistsWorkingDirectoryIsUsedOnlyWhenItIsACheckout() async throws {
        func locate(workingDirectory: String, files: [String: Data]) async throws -> KeryxLocation {
            var fs = FakeFileSystem(files: files)
            fs.files[Self.plistPath] = try Self.plist([
                "ProgramArguments": ["/Users/x/.local/bin/keryx", "serve"],
                "WorkingDirectory": workingDirectory,
            ])
            fs.executables = ["/Users/x/.local/bin/keryx"]
            let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)
            return try await locator.locate(preference: nil).get()
        }
        // A checkout moved or deleted since the service was installed: not a checkout.
        let stale = try await locate(workingDirectory: "/Users/x/gone", files: [:])
        #expect(stale.checkout == nil)
        #expect(stale.workingDirectory == "/")
        // Home, with no pyproject.toml: not a checkout either, and not where it runs.
        let home = try await locate(workingDirectory: "/Users/x", files: [:])
        #expect(home.checkout == nil)
        #expect(home.workingDirectory == "/")
        // A checkout: both.
        let checkout = try await locate(workingDirectory: "/Users/x/code/keryx",
                                        files: ["/Users/x/code/keryx/pyproject.toml": Self.pyproject])
        #expect(checkout.checkout == "/Users/x/code/keryx")
        #expect(checkout.workingDirectory == "/Users/x/code/keryx")
    }

    @Test func aPlistProgramThatIsNeitherKeryxNorUvFallsThrough() async throws {
        var fs = FakeFileSystem()
        fs.files[Self.plistPath] = try Self.plist([
            "ProgramArguments": ["/usr/bin/python3", "-m", "keryx", "serve"],
            "WorkingDirectory": "/Users/x/code/keryx",
        ])
        fs.files["/Users/x/code/keryx/pyproject.toml"] = Self.pyproject
        fs.executables = ["/usr/bin/python3", "/Users/x/.local/bin/uv"]
        let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)

        let failure = try #require(await locator.locate(preference: nil).failure)
        #expect(failure.lookedAt.contains { $0.contains("/usr/bin/python3") && $0.contains("neither keryx nor uv") })
    }

    @Test func aUvPlistMustRunAProject() async throws {
        for arguments in [["/Users/x/.local/bin/uv", "tool", "run", "keryx", "serve"],
                          ["/Users/x/.local/bin/uv", "--project", "/Users/x/code/keryx", "keryx"],
                          ["/Users/x/.local/bin/uv", "run", "keryx", "serve"]] {
            var fs = FakeFileSystem()
            fs.files[Self.plistPath] = try Self.plist([
                "ProgramArguments": arguments, "WorkingDirectory": "/Users/x/code/keryx",
            ])
            fs.files["/Users/x/code/keryx/pyproject.toml"] = Self.pyproject
            fs.executables = ["/Users/x/.local/bin/uv"]
            let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)

            let failure = try #require(await locator.locate(preference: nil).failure, "\(arguments)")
            #expect(failure.lookedAt.contains { $0.contains("uv run --project") }, "\(arguments)")
        }
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
        #expect(envCall.arguments == ["-lic", LoginShell.script])
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

    @Test func theServicesOwnCheckoutChosenRunsWithTheServicesEnvironment() async throws {
        for chosen in ["/Users/x/code/keryx", "/Users/x/code/keryx/"] {
            var fs = FakeFileSystem()
            fs.files[Self.plistPath] = try Self.fixture("agent.plist")
            fs.files["/Users/x/code/keryx/pyproject.toml"] = Self.pyproject
            fs.files["/Users/x/code/keryx//pyproject.toml"] = Self.pyproject
            fs.executables = ["/Users/x/.local/bin/uv"]
            let runner = Self.shell(env: ["PATH": "/login/bin", "KERYX_HOME": "/elsewhere"])
            let locator = KeryxLocator(fileSystem: fs, runner: runner, home: Self.home)

            let location = try await locator.locate(preference: chosen).get()
            #expect(location.source == .preference)
            #expect(location.environment["KERYX_HOME"] == "/Users/x/.config/keryx")
            #expect(location.environment["XDG_STATE_HOME"] == "/Users/x/.local/state")
            #expect(location.environment["PATH"] == "/Users/x/.local/bin:/opt/homebrew/bin:/usr/bin:/bin")
            // The service's uv, and no login shell asked.
            #expect(location.executable == "/Users/x/.local/bin/uv")
            #expect(runner.requests.isEmpty)
        }
    }

    @Test func anotherCheckoutChosenRunsWithTheLoginShellsEnvironment() async throws {
        var fs = FakeFileSystem()
        fs.files[Self.plistPath] = try Self.fixture("agent.plist")
        fs.files["/Users/x/code/keryx/pyproject.toml"] = Self.pyproject
        fs.files["/Users/x/other/pyproject.toml"] = Self.pyproject
        fs.executables = ["/Users/x/.local/bin/uv"]
        let runner = Self.shell(env: ["PATH": "/login/bin", "KERYX_HOME": "/elsewhere"])
        let locator = KeryxLocator(fileSystem: fs, runner: runner, home: Self.home)

        let location = try await locator.locate(preference: "/Users/x/other").get()
        #expect(location.checkout == "/Users/x/other")
        #expect(location.environment["KERYX_HOME"] == "/elsewhere")
    }

    // MARK: - What makes a folder a checkout

    @Test func theRealDistributionShapeIsAcceptedAsAChosenFolder() async throws {
        var fs = FakeFileSystem()
        fs.files["/Users/x/elsewhere/pyproject.toml"] = Self.pyproject
        fs.executables = ["/Users/x/elsewhere/.venv/bin/keryx"]
        let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)

        let location = try await locator.locate(preference: "/Users/x/elsewhere").get()
        #expect(location.source == .preference)
        #expect(location.checkout == "/Users/x/elsewhere")
    }

    @Test func theRealDistributionShapeIsAcceptedViaThePlistProject() async throws {
        var fs = FakeFileSystem()
        fs.files[Self.plistPath] = try Self.fixture("agent.plist")
        fs.files["/Users/x/code/keryx/pyproject.toml"] = Self.pyproject
        fs.executables = ["/Users/x/.local/bin/uv"]
        let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)

        let location = try await locator.locate(preference: nil).get()
        #expect(location.source == .launchAgent)
        #expect(location.checkout == "/Users/x/code/keryx")
    }

    @Test func theRealDistributionShapeIsAcceptedAsAnInferredVenvCheckout() async throws {
        var fs = FakeFileSystem()
        fs.files["/Users/x/code/keryx/pyproject.toml"] = Self.pyproject
        fs.executables = ["/Users/x/code/keryx/.venv/bin/keryx"]
        let runner = Self.shell(env: ["PATH": "/a:/b", "HOME": "/Users/x"],
                                keryx: "/Users/x/code/keryx/.venv/bin/keryx")
        let locator = KeryxLocator(fileSystem: fs, runner: runner, home: Self.home)

        let location = try await locator.locate(preference: nil).get()
        #expect(location.source == .loginShell)
        #expect(location.checkout == "/Users/x/code/keryx")
    }

    @Test func anUnrelatedProjectIsNotAKeryxCheckout() {
        var fs = FakeFileSystem()
        fs.files["/Users/x/other/pyproject.toml"] = Data("""
            [project]
            name = "other"
            version = "0.1.0"
            """.utf8)
        let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)
        #expect(locator.isKeryxCheckout("/Users/x/other") == false)
    }

    @Test func aKeryxKeyUnderAnUnrelatedTableIsNotAConsoleScript() {
        var fs = FakeFileSystem()
        fs.files["/Users/x/other/pyproject.toml"] = Data("""
            [project]
            name = "other"

            [tool.something]
            keryx = "not a console script"
            """.utf8)
        let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)
        #expect(locator.isKeryxCheckout("/Users/x/other") == false)
    }

    @Test func srcKeryxInitPyBesidePyprojectIsAlsoAccepted() {
        var fs = FakeFileSystem()
        fs.files["/Users/x/other/pyproject.toml"] = Data("[project]\nname = \"other\"\n".utf8)
        fs.files["/Users/x/other/src/keryx/__init__.py"] = Data()
        let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)
        #expect(locator.isKeryxCheckout("/Users/x/other") == true)
    }

    // MARK: - The login shell

    @Test func loginShellFindsAVenvKeryx() async throws {
        var fs = FakeFileSystem()
        fs.files["/Users/x/code/keryx/pyproject.toml"] = Self.pyproject
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
        #expect(location.workingDirectory == "/")
    }

    @Test func aVenvKeryxOutsideAKeryxCheckoutIsABareKeryx() async throws {
        var fs = FakeFileSystem()
        fs.files["/Users/x/other/pyproject.toml"] = Data("[project]\nname = \"other\"\n".utf8)
        fs.executables = ["/Users/x/other/.venv/bin/keryx"]
        let runner = Self.shell(env: ["PATH": "/a"], keryx: "/Users/x/other/.venv/bin/keryx")
        let locator = KeryxLocator(fileSystem: fs, runner: runner, home: Self.home)

        let location = try await locator.locate(preference: nil).get()
        #expect(location.executable == "/Users/x/other/.venv/bin/keryx")
        #expect(location.checkout == nil)
        #expect(location.workingDirectory == "/")
    }

    @Test func loginShellAliasIsNotAPath() async throws {
        let runner = Self.shell(env: ["PATH": "/a"], keryx: "alias keryx='uv run keryx'")
        let locator = KeryxLocator(fileSystem: FakeFileSystem(), runner: runner, home: Self.home)
        #expect(await locator.locate(preference: nil).failure != nil)
    }

    @Test func loginShellNoiseBeforeEnvIsIgnored() async throws {
        let runner = FakeRunner { request in
            if request.arguments.last == LoginShell.script {
                // A banner with no newline before the marker cannot run into the first name.
                return .init(exitCode: 0, stdout: Self.envOutput(["PATH": "/p", "HOME": "/Users/x"],
                                                                 before: "Welcome!\nPATH=/wrong"),
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

    @Test func theEnvironmentIsReadOnlyAfterTheMarker() {
        let merged = Data("WelcomePATH=/wrong\0HOME=/h\0".utf8)
        #expect(LoginShell.parse(merged) == nil)
        let output = Self.envOutput(["PATH": "/p"], before: "WelcomePATH=/wrong")
            + Data("Goodbye\n".utf8)
        #expect(LoginShell.parse(output) == ["PATH": "/p"])
    }

    @Test func uvToolInstallIsTheLastResort() async throws {
        var fs = FakeFileSystem()
        fs.executables = ["/Users/x/.local/bin/keryx"]
        let locator = KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home)

        let location = try await locator.locate(preference: nil).get()
        #expect(location.executable == "/Users/x/.local/bin/keryx")
        #expect(location.checkout == nil)
        #expect(location.prefixArguments == [])
        #expect(location.workingDirectory == "/")
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

    @Test func inheritSkipsTheLaunchAgent() async throws {
        var fs = FakeFileSystem()
        fs.files[Self.plistPath] = try Self.fixture("agent.plist")
        fs.files["/Users/x/code/keryx/pyproject.toml"] = Self.pyproject
        fs.executables = ["/Users/x/.local/bin/uv", "/sb/bin/keryx"]
        let runner = Self.noShell()
        let locator = KeryxLocator(
            fileSystem: fs, runner: runner, home: Self.home,
            environment: ["KERYX_APP_ENVIRONMENT": "inherit", "PATH": "/sb/bin",
                          "KERYX_HOME": "/sb/home/.config/keryx"])

        let location = try await locator.locate(preference: nil).get()
        #expect(location.source == .loginShell)
        #expect(location.executable == "/sb/bin/keryx")
        #expect(location.environment["KERYX_HOME"] == "/sb/home/.config/keryx")
        #expect(location.environment["XDG_STATE_HOME"] == nil)
        #expect(runner.requests.isEmpty)

        // With nothing on the PATH it fails rather than fall back to the real installation.
        var bare = fs
        bare.executables = ["/Users/x/.local/bin/uv"]
        let failing = KeryxLocator(
            fileSystem: bare, runner: Self.noShell(), home: Self.home,
            environment: ["KERYX_APP_ENVIRONMENT": "inherit", "PATH": "/sb/bin"])
        let failure = try #require(await failing.locate(preference: nil).failure)
        #expect(failure.lookedAt.contains { $0.contains("KERYX_APP_ENVIRONMENT=inherit") })
    }

    @Test func inheritStillLetsAChosenFolderWinOverThePlist() async throws {
        var fs = FakeFileSystem()
        fs.files[Self.plistPath] = try Self.fixture("agent.plist")
        // The chosen folder is the service's own checkout: under inherit, still the app's env.
        fs.files["/Users/x/code/keryx/pyproject.toml"] = Self.pyproject
        fs.executables = ["/Users/x/.local/bin/uv"]
        let locator = KeryxLocator(
            fileSystem: fs, runner: Self.noShell(), home: Self.home,
            environment: ["KERYX_APP_ENVIRONMENT": "inherit", "PATH": "/sb/bin",
                          "KERYX_HOME": "/sb/home/.config/keryx"])

        let location = try await locator.locate(preference: "/Users/x/code/keryx").get()
        #expect(location.source == .preference)
        #expect(location.environment["KERYX_HOME"] == "/sb/home/.config/keryx")
        #expect(location.environment["PATH"] == "/sb/bin")
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
        #expect(runner.requests.first?.arguments == ["-lic", LoginShell.script])
    }

    // MARK: - The plist remembered

    /// Quitting the app removes the plist with the unit: the next launch finds Keryx from the
    /// copy it last read, with the environment the service ran with.
    @Test func aUsablePlistIsRememberedAndReadWhileItIsGone() async throws {
        let memory = Preferences(defaults: freshDefaults())
        var fs = FakeFileSystem()
        let plist = try Self.fixture("agent.plist")
        fs.files[Self.plistPath] = plist
        fs.files["/Users/x/code/keryx/pyproject.toml"] = Self.pyproject
        fs.executables = ["/Users/x/.local/bin/uv"]
        let found = try await KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home, memory: memory)
            .locate(preference: nil).get()
        #expect(memory.recallLaunchAgent() == plist)

        fs.files[Self.plistPath] = nil
        let again = try await KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home, memory: memory)
            .locate(preference: nil).get()
        #expect(again == found)
        #expect(again.environment["KERYX_HOME"] == "/Users/x/.config/keryx")
    }

    @Test func withNothingRememberedAMissingPlistStillFallsThrough() async throws {
        let memory = Preferences(defaults: freshDefaults())
        let failure = await KeryxLocator(fileSystem: FakeFileSystem(), runner: Self.shell(), home: Self.home,
                                         memory: memory).locate(preference: nil).failure
        #expect(failure?.lookedAt.contains { $0.hasPrefix("There is no ~/Library/LaunchAgents/dev.keryx.agent.plist") } == true)
    }

    @Test func aRememberedPlistWhoseCheckoutIsGoneIsNotUsed() async throws {
        let memory = Preferences(defaults: freshDefaults())
        memory.rememberLaunchAgent(try Self.fixture("agent.plist"))
        // No pyproject.toml: the checkout it names was moved or deleted.
        let result = await KeryxLocator(fileSystem: FakeFileSystem(), runner: Self.shell(), home: Self.home,
                                        memory: memory).locate(preference: nil)
        #expect(result.failure != nil)
    }

    @Test func anUnusablePlistIsNeitherRememberedNorReplacedByTheMemory() async throws {
        let memory = Preferences(defaults: freshDefaults())
        let good = try Self.fixture("agent.plist")
        memory.rememberLaunchAgent(good)
        var fs = FakeFileSystem()
        fs.files[Self.plistPath] = try Self.plist(["ProgramArguments": ["/usr/bin/python3", "serve"]])
        fs.files["/Users/x/code/keryx/pyproject.toml"] = Self.pyproject
        fs.executables = ["/Users/x/.local/bin/uv"]
        let result = await KeryxLocator(fileSystem: fs, runner: Self.shell(), home: Self.home, memory: memory)
            .locate(preference: nil)
        #expect(result.failure != nil, "the plist there says what runs, even when it is not Keryx")
        #expect(memory.recallLaunchAgent() == good)
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
