import Foundation

/// Finds the `keryx` CLI the way its launchd service runs it. A GUI app inherits no shell
/// environment, and the service runs with its installer's `KERYX_HOME` and XDG homes, so a
/// bare `keryx` on some PATH would read different settings from the service's. In order:
///
/// 1. The owner's chosen folder (`preference`): a checkout, by its `pyproject.toml` — a
///    `keryx` console script in `[project.scripts]`, a `[project]` named `keryx` or
///    `keryx-voice`, or a `src/keryx/__init__.py` beside it. It runs with the login shell's
///    environment — or, when it is the service's own checkout, with the service's (its
///    `KERYX_HOME` and XDG homes).
/// 2. `~/Library/LaunchAgents/dev.keryx.agent.plist`: the service's own program, folder and
///    environment — or, while there is none, the copy of it last read (`memory`). Quitting
///    the app removes the plist with the unit, and the next launch must still find the
///    Keryx that installs it again, with the environment it ran with.
/// 3. The login shell: its environment, and `command -v keryx`; then `~/.local/bin/keryx`.
///
/// A checkout is optional: a `keryx` installed on its own runs with no prefix, from `/` (as
/// launchd runs it) — never from `HOME`, where an unrelated `.env` would make the CLI refuse.
/// Every step that does not do appends one sentence to `LocateFailure.lookedAt`.
///
/// `KERYX_APP_ENVIRONMENT=inherit` in the app's own environment replaces the login shell
/// with that environment, for both questions it answers (the environment, and where `keryx`
/// is on `PATH`), and skips the launch agent, whose environment is the real installation's:
/// only someone who started the binary from a shell can set it, and it is how a run against
/// a sandbox (`KERYX_HOME` and the XDG homes exported) reaches that sandbox and nothing else.
public struct KeryxLocator: Locating {
    static let agentPlist = "Library/LaunchAgents/dev.keryx.agent.plist"
    static let environmentOverride = "KERYX_APP_ENVIRONMENT"

    private let fileSystem: FileSystem
    private let runner: CommandRunner
    private let home: String
    private let environment: [String: String]
    private let memory: LaunchAgentMemory?

    /// `environment`: the app's own, `ProcessInfo`'s outside tests. `memory`: where a usable
    /// plist is kept, to be read while the plist itself is gone.
    public init(fileSystem: FileSystem = LocalFileSystem(), runner: CommandRunner = ProcessRunner(),
                home: String = NSHomeDirectory(),
                environment: [String: String] = ProcessInfo.processInfo.environment,
                memory: LaunchAgentMemory? = nil) {
        self.fileSystem = fileSystem
        self.runner = runner
        self.home = home
        self.environment = environment
        self.memory = memory
    }

    /// True when the app was started with `KERYX_APP_ENVIRONMENT=inherit`.
    private var inherits: Bool { environment[Self.environmentOverride] == "inherit" }

    public func locate(preference: String?) async -> Result<KeryxLocation, LocateFailure> {
        var lookedAt: [String] = []
        var shell = LoginShell(inherited: inherits ? inheritedEnvironment : nil)
        // Read once, before the preference needs it; never under `inherit`.
        let agent = inherits ? nil : readLaunchAgent()

        if let preference {
            if isKeryxCheckout(preference) {
                let environment: [String: String]
                let uv: String?
                if case let .success(service)? = agent, let checkout = service.checkout,
                   Self.samePath(checkout, preference) {
                    // The service's own checkout: what the service sees, not the login shell.
                    environment = service.environment
                    uv = service.uv
                } else {
                    environment = await shell.environment(runner: runner, base: processEnvironment)
                        ?? processEnvironment
                    uv = nil
                }
                if let location = forCheckout(
                    preference, uv: uv, environment: environment, source: .preference,
                    lookedAt: &lookedAt) {
                    return .success(location)
                }
            } else {
                lookedAt.append("The folder you chose, \(tilde(preference)), doesn't look like a Keryx checkout (no pyproject.toml with a keryx command).")
            }
        } else {
            lookedAt.append("No Keryx folder has been chosen in the app.")
        }

        switch agent {
        case nil:
            lookedAt.append("\(Self.environmentOverride)=inherit, so the launch agent Keryx runs under was not read.")
        case let .failure(reason):
            lookedAt.append(reason.sentence)
        case let .success(service):
            if let location = fromLaunchAgent(service, lookedAt: &lookedAt) {
                return .success(location)
            }
        }
        if let location = await fromLoginShell(&shell, lookedAt: &lookedAt) {
            return .success(location)
        }
        return .failure(LocateFailure(lookedAt: lookedAt))
    }

    // MARK: - The launch agent

    /// What the service's plist says, already checked: its program is `keryx`, or `uv run
    /// --project CHECKOUT` with a checkout that names keryx.
    private struct LaunchAgent {
        /// `CHECKOUT/…/keryx` or the like, for a `keryx` run directly; else `nil`.
        var keryx: String?
        /// The `uv` that runs the checkout; `nil` for a `keryx` run directly.
        var uv: String?
        /// A checkout, validated: `--project` for uv, a valid `WorkingDirectory` for keryx.
        var checkout: String?
        var environment: [String: String]
    }

    private struct Unusable: Error {
        var sentence: String
    }

    /// The plist, else the copy of it last read; a usable plist is remembered.
    private func readLaunchAgent() -> Result<LaunchAgent, Unusable> {
        let path = home + "/" + Self.agentPlist
        let name = tilde(path)
        guard let data = fileSystem.contents(atPath: path) else {
            if let remembered = memory?.recallLaunchAgent(),
               case let .success(agent) = launchAgent(from: remembered, name: name) {
                return .success(agent)
            }
            return .failure(Unusable(sentence: "There is no \(name), so Keryx has not run in the background here yet."))
        }
        let agent = launchAgent(from: data, name: name)
        if case .success = agent { memory?.rememberLaunchAgent(data) }
        return agent
    }

    private func launchAgent(from data: Data, name: String) -> Result<LaunchAgent, Unusable> {
        guard let plist = (try? PropertyListSerialization.propertyList(from: data, format: nil))
            as? [String: Any]
        else {
            return .failure(Unusable(sentence: "\(name) could not be read as a property list."))
        }
        let arguments = plist["ProgramArguments"] as? [String] ?? []
        guard let program = arguments.first else {
            return .failure(Unusable(sentence: "\(name) names no program to run."))
        }
        let workingDirectory = plist["WorkingDirectory"] as? String
        let declared = (plist["EnvironmentVariables"] as? [String: Any] ?? [:])
            .compactMapValues { $0 as? String }
        let environment = ["HOME": home, "USER": NSUserName()]
            .merging(declared) { _, plist in plist }

        switch (program as NSString).lastPathComponent {
        case "keryx":
            // A `keryx` run directly (a `uv tool install` or a PyPI install): no prefix, and
            // whatever follows it (`serve`) is the service's business, not ours. Its folder
            // is a checkout only if it still is one: a moved or deleted checkout is not.
            return .success(LaunchAgent(
                keryx: program, uv: nil,
                checkout: workingDirectory.flatMap { isKeryxCheckout($0) ? $0 : nil },
                environment: environment))
        case "uv":
            guard arguments.count > 1, arguments[1] == "run", let checkout = projectArgument(arguments)
            else {
                return .failure(Unusable(sentence: "\(name) runs uv, but not as uv run --project with a checkout."))
            }
            guard isKeryxCheckout(checkout) else {
                return .failure(Unusable(
                    sentence: "\(name) points at \(tilde(checkout)), which doesn't look like a Keryx checkout (no pyproject.toml with a keryx command)."))
            }
            return .success(LaunchAgent(keryx: nil, uv: program, checkout: checkout,
                                        environment: environment))
        default:
            return .failure(Unusable(sentence: "\(name) runs \(tilde(program)), which is neither keryx nor uv."))
        }
    }

    private func fromLaunchAgent(_ service: LaunchAgent, lookedAt: inout [String]) -> KeryxLocation? {
        if let keryx = service.keryx {
            guard fileSystem.isExecutableFile(atPath: keryx) else {
                lookedAt.append("\(tilde(home + "/" + Self.agentPlist)) runs \(tilde(keryx)), which is not there.")
                return nil
            }
            return KeryxLocation(
                checkout: service.checkout, executable: keryx, prefixArguments: [],
                environment: Sanitise.environment(service.environment),
                workingDirectory: service.checkout ?? KeryxLocation.noCheckoutDirectory,
                source: .launchAgent)
        }
        guard let checkout = service.checkout else { return nil }
        return forCheckout(checkout, uv: service.uv, environment: service.environment,
                           source: .launchAgent, lookedAt: &lookedAt)
    }

    /// The value after `--project` (or of `--project=…`), found by name, not position.
    private func projectArgument(_ arguments: [String]) -> String? {
        for (index, argument) in arguments.enumerated() {
            if argument == "--project", index + 1 < arguments.count {
                return arguments[index + 1]
            }
            if argument.hasPrefix("--project=") {
                return String(argument.dropFirst("--project=".count))
            }
        }
        return nil
    }

    // MARK: - The login shell

    private func fromLoginShell(_ shell: inout LoginShell, lookedAt: inout [String]) async -> KeryxLocation? {
        let environment = await shell.environment(runner: runner, base: processEnvironment)
            ?? processEnvironment

        if let path = await commandV(), path.hasPrefix("/"), fileSystem.isExecutableFile(atPath: path) {
            // A checkout's own venv — when the folder above it is one; any other project's
            // venv is only a `keryx` installed there.
            let checkout = dirname(dirname(dirname(path)))
            if path.hasSuffix("/.venv/bin/keryx"), isKeryxCheckout(checkout) {
                return KeryxLocation(
                    checkout: checkout, executable: path, prefixArguments: [],
                    environment: Sanitise.environment(environment),
                    workingDirectory: checkout, source: .loginShell)
            }
            return bare(path, environment: environment)
        }
        lookedAt.append(inherits ? "The PATH the app was started with has no keryx."
                                 : "Your login shell (zsh) has no keryx on its PATH.")

        let uvTool = home + "/.local/bin/keryx"
        if fileSystem.isExecutableFile(atPath: uvTool) {
            return bare(uvTool, environment: environment)
        }
        lookedAt.append("There is no \(tilde(uvTool)) (a uv tool install) either.")
        return nil
    }

    private func bare(_ path: String, environment: [String: String]) -> KeryxLocation {
        KeryxLocation(
            checkout: nil, executable: path, prefixArguments: [],
            environment: Sanitise.environment(environment),
            workingDirectory: KeryxLocation.noCheckoutDirectory, source: .loginShell)
    }

    /// `command -v keryx` in the login shell: the last line it printed, since an
    /// interactive profile may print a greeting first. Inheriting, the app's own `PATH`.
    private func commandV() async -> String? {
        if inherits {
            return (inheritedEnvironment["PATH"] ?? "")
                .split(separator: ":")
                .map { $0 + "/keryx" }
                .first { $0.hasPrefix("/") && fileSystem.isExecutableFile(atPath: $0) }
        }
        let request = CommandRequest(
            executable: "/bin/zsh", arguments: ["-lic", "command -v keryx"],
            environment: processEnvironment, workingDirectory: home, stdin: Data(),
            timeout: .seconds(5))
        guard let result = try? await runner.run(request), result.exitCode == 0 else { return nil }
        return result.stdoutText
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .last { !$0.isEmpty }
    }

    // MARK: - A checkout

    /// The checkout's own `.venv/bin/keryx` when there is one, else `uv run --project
    /// CHECKOUT --no-sync keryx` (never a plain `uv run`, which may sync the environment).
    private func forCheckout(
        _ checkout: String, uv plistUV: String?, environment: [String: String],
        source: KeryxLocation.Source, lookedAt: inout [String]
    ) -> KeryxLocation? {
        let venv = checkout + "/.venv/bin/keryx"
        let sanitised = Sanitise.environment(environment)
        if fileSystem.isExecutableFile(atPath: venv) {
            return KeryxLocation(
                checkout: checkout, executable: venv, prefixArguments: [],
                environment: sanitised, workingDirectory: checkout, source: source)
        }
        let candidates = [plistUV].compactMap { $0 }
            + [home + "/.local/bin/uv", "/opt/homebrew/bin/uv", "/usr/local/bin/uv"]
        guard let uv = candidates.first(where: { fileSystem.isExecutableFile(atPath: $0) }) else {
            lookedAt.append("\(tilde(checkout)) has no .venv/bin/keryx, and no uv was found to run it with.")
            return nil
        }
        return KeryxLocation(
            checkout: checkout, executable: uv,
            prefixArguments: ["run", "--project", checkout, "--no-sync", "keryx"],
            environment: sanitised, workingDirectory: checkout, source: source)
    }

    /// True when `folder` is a Keryx checkout: its `pyproject.toml` declares a `keryx`
    /// console script in `[project.scripts]`, or names the project `keryx` or `keryx-voice`
    /// in `[project]`; or `folder/src/keryx/__init__.py` exists alongside a `pyproject.toml`.
    /// What makes a folder Keryx's is the command it provides, not what the distribution is
    /// called — the real distribution is `keryx-voice`, with `keryx` as its console script.
    func isKeryxCheckout(_ folder: String) -> Bool {
        guard let data = fileSystem.contents(atPath: folder + "/pyproject.toml") else { return false }
        if fileSystem.contents(atPath: folder + "/src/keryx/__init__.py") != nil { return true }

        var table = ""
        for rawLine in String(decoding: data, as: UTF8.self).split(whereSeparator: \.isNewline) {
            let line = rawLine.split(separator: "#", maxSplits: 1, omittingEmptySubsequences: false)
                .first.map { $0.trimmingCharacters(in: .whitespaces) } ?? ""
            if line.hasPrefix("[") {
                table = line
                continue
            }
            let parts = line.split(separator: "=", maxSplits: 1).map {
                $0.trimmingCharacters(in: .whitespaces)
            }
            guard parts.count == 2 else { continue }
            if table == "[project]", parts[0] == "name" {
                if ["\"keryx\"", "'keryx'", "\"keryx-voice\"", "'keryx-voice'"].contains(parts[1]) {
                    return true
                }
            } else if table == "[project.scripts]", parts[0] == "keryx" {
                return true
            }
        }
        return false
    }

    // MARK: - Helpers

    private var processEnvironment: [String: String] {
        environment.merging(["HOME": home]) { _, home in home }
    }

    /// The app's environment as it was started, `HOME` included; `home` only when it has none.
    private var inheritedEnvironment: [String: String] {
        environment.merging(["HOME": home]) { own, _ in own }
    }

    /// The same folder, written either way (a trailing slash, a `.`).
    private static func samePath(_ a: String, _ b: String) -> Bool {
        (a as NSString).standardizingPath == (b as NSString).standardizingPath
    }

    private func tilde(_ path: String) -> String {
        path.hasPrefix(home + "/") ? "~" + path.dropFirst(home.count) : path
    }

    private func dirname(_ path: String) -> String {
        (path as NSString).deletingLastPathComponent
    }
}

/// The login shell's environment, asked for at most once per `locate` — or, `inherited`,
/// never asked at all.
struct LoginShell {
    /// What the shell runs: a marker between two NULs, then `env -0`. Whatever an
    /// interactive profile prints comes before the marker, newline or not, and is dropped.
    static let marker = "keryx-environment"
    static let script = "printf '\\0%s\\0' \(marker); env -0"

    private var fetched = false
    private var cached: [String: String]?

    init(inherited: [String: String]?) {
        if let inherited {
            fetched = true
            cached = inherited
        }
    }

    mutating func environment(runner: CommandRunner, base: [String: String]) async -> [String: String]? {
        if fetched { return cached }
        fetched = true
        let request = CommandRequest(
            executable: "/bin/zsh", arguments: ["-lic", Self.script], environment: base,
            workingDirectory: base["HOME"], stdin: Data(), timeout: .seconds(5))
        if let result = try? await runner.run(request), result.exitCode == 0 {
            cached = Self.parse(result.stdout)
        }
        return cached
    }

    /// The `KEY=value` entries after the marker, each ended by a NUL; `nil` with no marker
    /// (nothing printed can be told from a profile's noise then). An entry whose name is not
    /// a variable name (a logout script's farewell, say) is dropped.
    static func parse(_ data: Data) -> [String: String]? {
        let marker = Data([0] + Array(Self.marker.utf8) + [0])
        guard let range = data.range(of: marker) else { return nil }
        var environment: [String: String] = [:]
        for entry in data[range.upperBound...].split(separator: 0) {
            let text = String(decoding: entry, as: UTF8.self)
            guard let equals = text.firstIndex(of: "=") else { continue }
            let name = text[..<equals]
            guard let first = name.first, first == "_" || first.isLetter,
                  name.allSatisfy({ $0 == "_" || ($0.isASCII && ($0.isLetter || $0.isNumber)) })
            else { continue }
            environment[String(name)] = String(text[text.index(after: equals)...])
        }
        return environment.isEmpty ? nil : environment
    }
}
