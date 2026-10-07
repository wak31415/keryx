import Foundation
import KeryxKit

/// The sample documents under `Sources/KeryxPreview/Preview/`: the CLI's own `--json` output,
/// captured from a seeded sandbox store with its paths scrubbed to `/Users/you`, edited by
/// hand only for a state no sandbox has (a running service, a running llama-server). They
/// are bundled resources, read through `Bundle.module`, so previews, snapshots and this
/// package's own tests all read the one copy rather than three.
///
/// A fixture failing to load or decode is this package's own bug, not a caller's mistake —
/// so every accessor crashes with a clear message instead of returning an optional or
/// throwing, the same stance Swift's own `Bundle.module` takes on a resource it expects to
/// find.
public enum PreviewData {
    public static func data(_ name: String) -> Data {
        guard let url = Bundle.module.url(forResource: name, withExtension: nil, subdirectory: "Preview") else {
            fatalError("PreviewData: no such fixture 'Preview/\(name)'")
        }
        guard let data = try? Data(contentsOf: url) else {
            fatalError("PreviewData: could not read fixture 'Preview/\(name)'")
        }
        return data
    }

    /// `pull.jsonl` as its individual lines, for feeding one at a time to `PullEvent.parse`.
    public static var pullLines: [String] {
        String(decoding: data("pull.jsonl"), as: UTF8.self)
            .split(separator: "\n", omittingEmptySubsequences: true)
            .map(String.init)
    }

    public static var status: StatusReport { decode("status.json") }
    public static var statusMigration: StatusReport { decode("status-migration.json") }
    public static var doctor: DoctorReport { decode("doctor.json") }
    public static var tasks: TaskList { decode("tasks.json") }
    public static var taskDetail: TaskDetail { decode("task-detail.json") }
    public static var usage: UsageReport { decode("usage.json") }
    public static var models: ModelsReport { decode("models.json") }
    public static var modelsEmptyServers: ModelsReport { decode("models-empty-servers.json") }
    public static var config: [ConfigRow] { decode("config.json") }
    public static var approvals: ApprovalsReport { decode("approvals.json") }
    public static var memory: MemoryDocument { decode("memory.json") }
    /// `voice --json` captured from a sandbox: OpenAI with a local model for the words, and
    /// a server elsewhere that is not answering; with nothing set, needing a local model.
    /// `voice-here.json` is that first one edited by hand into a running voice server here.
    public static var voice: VoiceStatus { decode("voice.json") }
    public static var voiceHere: VoiceStatus { decode("voice-here.json") }
    public static var voiceServer: VoiceStatus { decode("voice-server.json") }
    public static var voiceNeedsModel: VoiceStatus { decode("voice-needs-model.json") }

    /// `voice-here.jsonl` as its individual lines: a setup's steps, written by hand (a
    /// sandbox cannot install the voice server). uv reports no sizes, so no step has any.
    public static var voiceHereLines: [String] { lines("voice-here.jsonl") }

    /// `voice use here` refused for want of a local model, captured from a sandbox.
    public static var voiceHereRefusedLines: [String] { lines("voice-here-refused.jsonl") }

    // MARK: Setup, phone, service, accounts and plugins

    /// `setup plan --json`: a new machine (sandbox), one part way (sandbox: the owner, the
    /// PIN and a skipped step done, a plugin refused), and one ready for a first call
    /// (hand-edited from the part-way plan: a sandbox has no key, no agent and no Twilio).
    public static var setupPlan: SetupPlan { decode("setup-plan.json") }
    public static var setupPlanPartway: SetupPlan { decode("setup-plan-partway.json") }
    public static var setupPlanReady: SetupPlan { decode("setup-plan-ready.json") }

    /// `pin --json`: none yet (sandbox), one kept in its file (sandbox), and — written by
    /// hand — locked after wrong guesses, and one in the environment.
    public static var pinNone: PinStatus { decode("pin.json") }
    public static var pinFile: PinStatus { decode("pin-file.json") }
    public static var pinLocked: PinStatus { decode("pin-locked.json") }
    public static var pinEnvironment: PinStatus { decode("pin-environment.json") }

    /// `service --json` in the faked run's shape, its units edited by hand: Keryx and its
    /// tunnel running, the model server installed and stopped, no voice server.
    public static var service: ServiceReport { decode("service.json") }

    /// `phone numbers --json`: the faked account's number, and one more by hand that
    /// already points here; and with nothing saved to ask Twilio with (sandbox).
    public static var phoneNumbers: PhoneNumbers { decode("phone-numbers.json") }
    public static var phoneNumbersUnset: PhoneNumbers { decode("phone-numbers-unset.json") }
    /// `phone webhooks --json`: pointed elsewhere (faked run), nothing saved (sandbox), and
    /// already pointed here (hand-edited).
    public static var phoneWebhooks: PhoneWebhooks { decode("phone-webhooks.json") }
    public static var phoneWebhooksUnset: PhoneWebhooks { decode("phone-webhooks-unset.json") }
    public static var phoneWebhooksMatching: PhoneWebhooks { decode("phone-webhooks-matching.json") }
    /// `phone tunnel --json`: cloudflared answering at its host (faked run; `reachable` by
    /// hand), and no host and no tunnel (sandbox).
    public static var phoneTunnel: PhoneTunnel { decode("phone-tunnel.json") }
    public static var phoneTunnelUnset: PhoneTunnel { decode("phone-tunnel-unset.json") }

    /// `integrations --json` from the sandbox: Slack connected, the rest not.
    public static var integrations: IntegrationsReport { decode("integrations.json") }
    /// `plugins list --json` from the sandbox: Slack on, Email answers installed and
    /// refused, Billing on and not ready, Cluster stats off.
    public static var plugins: PluginsReport { decode("plugins.json") }
    /// `tools --json` from the sandbox: the plugins' tools and two of the owner's, one file
    /// that will not import and one plugin refused.
    public static var tools: ToolsReport { decode("tools.json") }
    /// `plugins hosts --json`, written by hand: `ssh -G` reads the real ~/.ssh/config
    /// whatever HOME says, so a sandbox cannot give it.
    public static var sshHosts: SshHostsReport { decode("ssh-hosts.json") }

    // MARK: Streams

    /// `models start --json`: llama.cpp installed (its sizes unknown), its unit, the model
    /// loaded and checked — the faked run with its runtime download added by hand.
    public static var modelsStartLines: [String] { lines("models-start.jsonl") }
    /// `models start --json` with no model chosen: refused before anything (faked run).
    public static var modelsStartRefusedLines: [String] { lines("models-start-refused.jsonl") }
    /// `voice download --json`: the faked run, with real sizes put in by hand.
    public static var voiceDownloadLines: [String] { lines("voice-download.jsonl") }

    /// `auth login NAME --json` signing in, from the faked runs (their one-off state and
    /// challenge replaced).
    public static func signInLines(_ provider: SignInProvider) -> [String] {
        lines("login-\(provider.rawValue).jsonl")
    }

    private static func lines(_ name: String) -> [String] {
        String(decoding: data(name), as: UTF8.self)
            .split(separator: "\n", omittingEmptySubsequences: true)
            .map(String.init)
    }

    /// The `config options` documents there are fixtures for, by file, one per key. The
    /// voice, Claude, Codex, catalog, Kokoro and speech model lists are captured from a sandbox (the
    /// catalog's "fits" edited to match `models.json`'s Mac); the live OpenAI and
    /// model-server lists, which a sandbox with no key and no server cannot give, are
    /// written by hand.
    public static let optionsFixtures = [
        "options-realtime.json", "options-transcription.json", "options-voice.json",
        "options-subagent.json", "options-codex.json", "options-local-agent.json", "options-llm-server.json",
        "options-kokoro.json", "options-stt.json", "options-tts.json",
    ]

    /// The Realtime models with no OpenAI key to ask with: the built-in list and why,
    /// captured from a sandbox.
    public static var realtimeFallback: ConfigOptions { decode("options-realtime-fallback.json") }

    /// Each `config options` fixture, by key.
    public static var options: [String: ConfigOptions] {
        var byKey: [String: ConfigOptions] = [:]
        for name in optionsFixtures {
            let list: ConfigOptions = decode(name)
            byKey[list.key] = list
        }
        return byKey
    }

    /// What `config options` says for a key with no fixture: the built-in list, holding only
    /// the value the setting has in `config.json`.
    public static func builtinOptions(_ key: String) -> ConfigOptions {
        let current = config.first { $0.key == key }?.value.flatMap { value -> String? in
            if case let .string(text) = value, !text.isEmpty { return text }
            return nil
        }
        return ConfigOptions(key: key, current: current,
                             options: current.map { [ConfigOption(value: $0, label: $0, detail: "current")] } ?? [],
                             source: "builtin", live: false, freeForm: true, error: nil)
    }

    private static func decode<T: Decodable>(_ name: String) -> T {
        do {
            return try JSON.decoder.decode(T.self, from: data(name))
        } catch {
            fatalError("PreviewData: could not decode 'Preview/\(name)' as \(T.self): \(error)")
        }
    }
}
