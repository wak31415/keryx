import KeryxKit

/// The app's own words for the settings whose description, as `keryx config list` gives it,
/// is written for a terminal: a command to run, a folder's variable, a script. The CLI's
/// descriptions also make `docs/configuration.md`, so they stay as they are; under a row's
/// title the app says these instead, and the tooltip keeps the CLI's whole, with the key.
/// Keys named here (`OPENAI_API_KEY`) are said by their titles, as any description's are.
enum ConfigDescriptions {
    static let overrides: [String: String] = [
        "AGENTS_ENABLED":
            "Every agent a task may be sent to. Keryx won't start with a default agent that isn't among them.",
        "CLAUDE_CODE_OAUTH_TOKEN":
            "Claude on your subscription, for a machine with no browser. Blank uses Claude's own stored sign-in.",
        "CODEX_API_KEY":
            "Codex, paid per token. Keryx signs Codex in with it once, in a folder of its own; "
            + "OPENAI_API_KEY is never used for Codex.",
        "CODEX_ACCESS_TOKEN":
            "Codex on your ChatGPT plan, for a machine with no browser. Blank uses Codex's own stored sign-in.",
        "VOICE_BASE_URL":
            "A voice server of your own that speaks the OpenAI Realtime protocol, as its …/v1 address. Blank is OpenAI.",
        "AGENT_SERVER_MODEL":
            "The model the local agent's llama.cpp server runs: one from Local Models, or a GGUF file's path. "
            + "Blank runs none.",
        "VOICE_MODE":
            "Where a call's voice runs: OpenAI Realtime, the speech pipeline on this Mac, or a Realtime server "
            + "elsewhere. Blank is worked out from VOICE_BASE_URL.",
        "VOICE_LLM_MODEL":
            "The voice LLM's model: on this Mac, one from Local Models or a GGUF file's path; on a server elsewhere, "
            + "that server's own name for it. Not read while the voice shares the local agent's model.",
        "VOICE_SERVER_STT": "How the local voice server hears: the speech-to-text model it runs.",
        "VOICE_SERVER_TTS": "How the local voice server speaks: the text-to-speech model it runs.",
        "VOICE_SERVER_VOICE":
            "The Kokoro voice the local voice server speaks in. A voice server elsewhere is sent it too, once it is set.",
        "VOICE_SERVER_ARGS":
            "More options for the local voice server, such as a smaller speech model or a speaking speed. "
            + "Never a key: other programs on this Mac can read them.",
        "GOOGLE_CLIENT_SECRETS_FILE":
            "The OAuth client file the Google Cloud console downloads. Blank uses the one saved when you "
            + "connected Google.",
        "USER_GOOGLE_EMAIL": "The Google account agents act as, filled in when you sign in.",
        "ISSUE_REPORTING":
            "Whether a bug or a feature request for Keryx, said on a call, may be filed as a GitHub issue by a "
            + "subagent. Only you can turn it on.",
        "APPROVALS_ENABLED":
            "Whether a Claude Code prompt left unanswered on your screen may become a call. It needs Keryx's "
            + "Claude Code hook, installed separately.",
        "APPROVAL_CALL_WINDOW_SECONDS":
            "How long the hook keeps waiting after that. Claude Code's own time limit for the hook must be longer "
            + "than the two together.",
        "APPROVAL_BASH_ALLOW":
            "The only shell commands a keypad digit may ever run, matched word for word; only git push and git "
            + "commit may carry arguments. Widening this list widens exactly what a phone keypad can run.",
        "CLOUDFLARE_TUNNEL": "The Cloudflare tunnel that runs beside Keryx in the background.",
        "SERVICE_MANAGER":
            "What restarts Keryx when it asks to be restarted: auto is launchd or systemd, but only while Keryx "
            + "runs under it; none refuses.",
        "SERVICE_UNIT": "The launchd label or systemd unit Keryx runs as in the background; blank is the one Keryx installs.",
        "REPORT_SECRET": "Signs the report links texted to you; blank makes one and keeps it with Keryx's data.",
        "LOG_LEVEL": "How much Keryx's log says.",
        "PROJECTS_ROOT":
            "Where a task with no project starts; each folder in it is a project you can name. Never created: "
            + "without it, such a task starts in Keryx's own workspace.",
        "DEBUG_SKIP_TWILIO_VALIDATION":
            "Skip Twilio's request signatures, for a machine nothing outside can reach. Keryx won't answer the "
            + "phone with it on while PUBLIC_HOST is set.",
        "DEMO_MODE":
            "Try the phone before a coding agent is set up: calls, the PIN and call-backs are real, and every task "
            + "comes back after a moment with a sample answer that says it was a demo. No agent runs and no agent "
            + "tokens are spent.",
    ]

    /// What to say under `row`'s title: the app's own words when it has them, else the CLI's.
    static func text(_ row: ConfigRow) -> String {
        overrides[row.key] ?? row.description
    }
}
