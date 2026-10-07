import Foundation

/// How to run the `keryx` CLI on this machine: what to exec, with what in front of the
/// subcommand, in which folder and environment — resolved the way the service resolves it.
public struct KeryxLocation: Sendable, Equatable {
    public enum Source: String, Sendable { case preference, launchAgent, loginShell }

    /// The checkout, an absolute path; `nil` for a `keryx` installed on its own (a
    /// `uv tool install` or a PyPI install), which has none.
    public var checkout: String?
    /// What to exec: `CHECKOUT/.venv/bin/keryx`, `uv`, or a bare `keryx` executable.
    public var executable: String
    /// `[]`, or `["run", "--project", CHECKOUT, "--no-sync", "keryx"]` in front of `uv`.
    public var prefixArguments: [String]
    /// Already passed through `Sanitise.environment`.
    public var environment: [String: String]
    /// The checkout (the CLI's `storage_refusal` looks at the cwd), else `/`.
    public var workingDirectory: String
    public var source: Source

    public init(
        checkout: String?, executable: String, prefixArguments: [String],
        environment: [String: String], workingDirectory: String, source: Source
    ) {
        self.checkout = checkout
        self.executable = executable
        self.prefixArguments = prefixArguments
        self.environment = environment
        self.workingDirectory = workingDirectory
        self.source = source
    }

    /// Where a `keryx` with no checkout runs: `/`, as launchd runs it. Never `HOME`: the
    /// CLI refuses to run beside a `.env`, and an unrelated one there would read as a Keryx
    /// waiting for `keryx migrate` — which would then import it.
    public static let noCheckoutDirectory = "/"

    /// The folder a command runs in: the checkout's, else `noCheckoutDirectory` — whatever
    /// `workingDirectory` says, so no location without a checkout runs anywhere else.
    public var runDirectory: String {
        checkout == nil ? Self.noCheckoutDirectory : workingDirectory
    }
}

/// Where the locator looked and why each place did not do, one sentence per step.
public struct LocateFailure: Error, Sendable, Equatable {
    public var lookedAt: [String]

    public init(lookedAt: [String]) {
        self.lookedAt = lookedAt
    }
}

public protocol Locating: Sendable {
    func locate(preference: String?) async -> Result<KeryxLocation, LocateFailure>
}

/// The environment every `keryx` the app runs gets. The app is the owner at a terminal and
/// never the service, so neither actor variable survives (a `KERYX_ACTOR=service` would
/// bind the owner to what the service may change); and the output is plain, wide text.
public enum Sanitise {
    public static func environment(_ env: [String: String]) -> [String: String] {
        var result = env
        result.removeValue(forKey: "KERYX_ACTOR")
        result.removeValue(forKey: "JARVIS_ACTOR")
        result["NO_COLOR"] = "1"
        result["TERM"] = "dumb"
        result["COLUMNS"] = "200"
        return result
    }
}
