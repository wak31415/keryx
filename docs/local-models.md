# Local and self-hosted models

Keryx can run the work and the voice on your own hardware: a model on this machine, or on
another one you can reach over your network or a tailnet. Each is an address:

| | Blank (the default) | Your own |
|---|---|---|
| The voice on a call | OpenAI's Realtime API (`OPENAI_API_KEY`) | `VOICE_BASE_URL`, any server that speaks the Realtime protocol |
| The work a call hands off | Claude Code or Codex, on Anthropic's or OpenAI's models | `LOCAL_AGENT_BASE_URL`: the `local` agent, Claude Code or Codex driving your model |

You can have either, both, or neither, and you can mix them: a local voice with Claude doing
the work, or the Realtime voice with tasks on your own GPU.

## The easy way: `keryx setup`

`uv run keryx setup`, then **Local models**. It asks where the agent's model should run and
where the voice should, and then:

- **On this machine**, it shows what the machine holds — an NVIDIA card's memory, a Mac's
  unified memory, the free disk — and offers a short list of open models that call tools
  well, with what fits marked and the best one recommended. It downloads the model from
  Hugging Face (resumable, checked against its SHA-256), or links a copy a Hugging Face tool
  already downloaded. It runs the model under llama.cpp, installing it when it is missing,
  or under Ollama when that is what you have. Then one real task proves it works.
- **A local voice** is Hugging Face's [speech-to-speech](https://github.com/huggingface/speech-to-speech):
  voice activity detection, speech-to-text, your model for the words, and Kokoro for the
  voice. Setup installs it, lets you pick a voice (Jarvis gets a British one), starts it,
  and makes it the voice only once it has answered.
- **A server elsewhere** is its address and an optional key, checked before it is kept.

Both servers on this machine run as user services of their own, bound to `127.0.0.1`:
`keryx-llm` and `keryx-voice` under systemd (`dev.keryx.llm` and `dev.keryx.voice` under
launchd), installed with `scripts/install-systemd.sh --llm --voice`. Each runs
`keryx models serve llm|voice`, which reads your settings, so a change is a
`keryx config set` and a restart of the unit.

From the command line:

```bash
uv run keryx models list            # what fits this machine, the recommended one marked, the voices
uv run keryx models pull qwen3-coder-30b-a3b
uv run keryx models use qwen3-coder-30b-a3b   # --default-agent to have it do unnamed work too
uv run keryx models start            # llama.cpp if missing, the model server's unit, a check
uv run keryx restart                 # Keryx reads the new settings when it starts
uv run keryx auth status --smoke     # one real task on each enabled agent

uv run keryx voice                   # where the voice is, and the voice server on this machine
uv run keryx voice use here          # --voice bm_george; installs, starts and waits for it
uv run keryx voice use server http://gpu-box:8765 --key-stdin   # a new key, on stdin
uv run keryx voice use openai        # back to OpenAI's Realtime API
```

`keryx voice use here` is the wizard's local voice, step for step (the same code): it needs
the local agent's model first, for the words, and a clone of Keryx, for the unit's installer.
`keryx voice use server` saves a server only once it has opened a Realtime session, unless
`--keep-anyway`. The key already saved is kept and asked with, unless `--key-stdin` replaces
it or `--clear-key` forgets it. Either way Keryx takes the new voice up when it restarts.

`keryx models use` saves exactly what `keryx setup` would for the same model — the model
server's model and port (a free one, the first time), the local agent's address and model,
and `local` among `AGENTS_ENABLED` — and refuses a model that is not downloaded yet.
`keryx models start` then does what the wizard does next, through the same code: it installs
llama.cpp when the machine has none (with Homebrew on a Mac that has it, else its pinned
release build), installs the model server's unit, waits for the model to load, and checks
that the server names the chosen model. A server that does not come up, answers with
another model, or is cancelled while it loads has its unit removed rather than left
restarting; its log is `keryx-llm.err.log`. `--runtime ollama` pulls Ollama's own copy of the
chosen model and points the local agent at it instead, and removes the llama.cpp server's
unit if one is installed (the wizard does the same). `keryx models stop` removes the unit, and `keryx service`
shows Keryx's, its tunnel's and both servers' units, installed and running.

For a program driving these: `keryx models list --json` adds `servers` (the model server,
the voice server and the local agent: whether each unit is installed, and whether a server
on this machine answers — a server elsewhere is never asked). `keryx models pull NAME --json`
prints JSON lines: `{"event": "progress", "done": …, "total": …}`, then exactly one of
`done` (with its `path`), `adopted` (`path` and `from`), `error` (`message`) or `cancelled` —
a refusal before the download included. A SIGTERM or SIGINT cancels it with exit 130, and
the partial download stays, so the next pull resumes it. `keryx models use NAME --json`
prints `{"saved": …, "llama_server": …, "restart_needed": true}`. `keryx models start --json`
prints `{"event": "step", "step": …, "message": …}` before each of `runtime`, `unit`, `start`
and `check`, `{"event": "progress", "step": "runtime", "done": …, "total": …}` while
llama.cpp downloads (`total` null when the server does not say), then exactly one of `done`
(`url`, `served_model`), `error` (`message`) or `cancelled`: exit 0, 1, 2 for a refusal
before anything is tried (no model chosen and downloaded, no checkout), 130 for a SIGTERM.
`keryx service --json` is `{"manager", "units": {"agent", "tunnel", "llm", "voice"}}`, each
`{"installed", "active"}`; `keryx service install --json`, `keryx service uninstall [--llm]
[--voice] --json` and `keryx models stop --json` print `{"ok": true, "message": …}`. `keryx
service install --llm` or `--voice` is refused with exit 2 and `{"ok": false, "message": …}`:
the model servers' units are installed by `keryx models start` and `keryx voice use here`,
which start the server, check it answers, and remove the unit again when it does not. From a
terminal, `scripts/install-launchd.sh --llm` (or `install-systemd.sh`) still installs one bare.

`keryx voice --json` is `mode` (`openai`, `here` or `server`), `base_url`, `realtime_model`,
`voice`, `assistant`, `openai_key_set`, `here` (speech-to-speech on this machine: `installed`,
`program`, `voice`, `port`, `unit_installed`, `reachable` — asked on loopback only — and
`words_model`), `needs` (`local_agent_model` while there is no local model for its words) and
`server.reachable` (asked only while a server elsewhere is the voice). `keryx voice use openai
--json` prints `{"saved": …, "restart_needed": true}`, and `voice use server URL --json` adds
`problem` and `warnings` — with a problem and no `--keep-anyway` it saves nothing, says
`"saved": null`, and exits 1. A key is shown as `(set)`, never itself. `keryx voice use here
--json` prints JSON lines: `{"event": "step", "step": …, "message": …}` before each slow step
(`install`, `configure`, `unit`, `start`; a warning is a step line too), then exactly one of
`done` (with its `base_url`), `error` (`message`) or `cancelled`. It exits 2 for a refusal
before anything is tried (no local model, no installer, a migration still to run), 1 for what
stopped it — the message names the server's log when it did not come up — and 130 for a
SIGTERM, which leaves a half-done install for the next run.

## What an address is

The way every OpenAI-compatible client describes a server: the `…/v1` root, an optional
key, and the server's own name for the model. A bare host gets `/v1` added
(`http://gpu-box:11434` is `http://gpu-box:11434/v1`); any other path is kept as given.

A key is sent as `Authorization: Bearer …`, the convention vLLM, llama.cpp, SGLang, TGI,
LocalAI and LM Studio all follow. A server that checks none — Ollama has none — is sent a
placeholder, because the SDKs refuse an empty key.

## The local agent

`AGENT_BACKEND=local` (or "have the local model do it" on a call, once `AGENTS_ENABLED`
lists it) runs a task on your model, inside one of the two harnesses Keryx already drives:

| `LOCAL_AGENT_API` | Driven by | Your server must serve |
|---|---|---|
| `anthropic-messages` (default) | Claude Code | `/v1/messages` |
| `openai-responses` | Codex | `/v1/responses` |

| Server | Messages API | Responses API | Example address |
|---|:---:|:---:|---|
| llama.cpp (`llama-server --jinja`) | ✅ | ✅ | `http://127.0.0.1:8080` |
| Ollama | ✅ 0.14 and later | ✅ 0.13.3 and later | `http://127.0.0.1:11434` |
| LM Studio | ✅ 0.4.1 and later | | `http://127.0.0.1:1234` |
| vLLM | 🟡 fragile | ✅ | `http://gpu-box:8000` |

On another machine it is the same, with that machine's address: `http://gpu-box:11434`,
`http://100.101.102.103:8000` on a tailnet, or `https://llm.example.com` behind a proxy that
checks a key (`LOCAL_AGENT_API_KEY`, set with `keryx config set … --stdin`).

What changes for a local task:

- **Your Anthropic and OpenAI credentials never reach it.** Claude Code is given the
  server's address and its key in place of yours, with both of yours blanked and every model
  family it might ask for set to your model. Codex is given the server as a model provider
  of the task's own, in a Codex home of Keryx's (`~/.local/share/keryx/codex-local`), so your
  ChatGPT login is never involved.
- **No dollar figure and no dollar cap.** The Claude SDK prices every token at Anthropic's
  rates, a local model's included, so the cost is dropped. The turn cap and the wall-clock
  cap still apply.
- **How well it goes is the model's.** The agents' system prompts are large (about 18,000
  tokens before any work), and a small model may not follow the `SPOKEN_SUMMARY:` line or
  call tools reliably. `keryx setup`'s smoke test is the quick check.
- **Gmail and Calendar** need Google for agents (`GOOGLE_WORKSPACE_MCP`), as for Codex: the
  claude.ai connectors belong to the Anthropic account.

The memory update runs on the default agent, so with `AGENT_BACKEND=local` it stays on your
machine too. The `check_email` plugin's summary still runs on the Claude CLI.

## The local voice

`VOICE_BASE_URL` points Keryx's one Realtime client at another server. The protocol is the
same; two things differ, and Keryx handles both:

- **Audio.** speech-to-speech reads every input as 16-bit PCM, so a phone call — µ-law at
  8 kHz — is converted to PCM at 24 kHz on the way in and back on the way out.
- **Notes to the model.** It treats a system message as a replacement for the whole system
  prompt, so an announcement ("task 4 is done") goes in as a note marked as not the caller's
  words.

And three you will notice:

- **Interruptions land late.** Its turn detection (Silero and Smart Turn) decides a turn has
  begun about a second or two after you start talking, so the assistant stops a little later
  than on the Realtime API. After an interruption the conversation keeps the whole sentence it
  was saying, not just the part you heard.
- **The voice is the server's.** OpenAI's voice names mean nothing there, so none is sent;
  pick one with `VOICE_SERVER_VOICE` (`keryx models list` shows Kokoro's), or `OPENAI_VOICE`
  for a server of your own that takes one.
- **No web search** unless `OPENAI_API_KEY` is still set: the assistant's `web_search` tool
  is OpenAI's Responses API.

On one GPU the voice and the agent share one model; setup keeps 3 GB free for the voice's own
speech models when it picks one. The voice server holds two calls at once, so a reconnect
does not find its only slot still taken.

The keypad PIN, approvals and call-backs are Twilio's and Keryx's, and work the same. A
spoken PIN is heard by your speech-to-text.

## Security

- **Both addresses are protected settings**: neither the voice model's `set_config` nor a
  subagent can change them. A voice address pointed elsewhere would carry every call — the
  spoken PIN included — to whoever is there.
- **The voice server hears everything.** Off this machine, use a tailnet or `https`: plain
  `http://` across your network carries the call audio unencrypted, and `keryx doctor` warns
  about it. speech-to-speech checks no key, so keep it on `127.0.0.1` (setup does) or behind
  Tailscale or a proxy that checks one.
- **A public address with no key is warned about, not refused** — it may be behind a firewall
  Keryx cannot see.

See [SECURITY.md](../SECURITY.md).

## What has been tested

| Model | Server | Agent: Claude Code | Agent: Codex | Voice |
|---|---|:---:|:---:|:---:|
| Qwen3-Coder 30B-A3B (Q4_K_M) | llama.cpp, RTX 5090 | ✅ | ✅ | ✅ speech-to-speech 1.0.0, Whisper and Kokoro |

The other models `keryx models list` offers are current open models that call tools, with
verified files; they have not run Keryx end to end yet. If one works well, or badly, an issue
saying so is welcome.

## Settings

| Setting | |
|---|---|
| `VOICE_BASE_URL`, `VOICE_API_KEY` | a voice server of your own; blank is OpenAI |
| `LOCAL_AGENT_BASE_URL`, `LOCAL_AGENT_API_KEY`, `LOCAL_AGENT_MODEL`, `LOCAL_AGENT_API` | the `local` agent's server, key, model and harness |
| `AGENT_SERVER_MODEL`, `AGENT_SERVER_PORT` | the model `keryx-llm` serves here, and its port |
| `VOICE_SERVER_PORT`, `VOICE_SERVER_STT`, `VOICE_SERVER_TTS`, `VOICE_SERVER_VOICE`, `VOICE_SERVER_ARGS` | the voice server `keryx-voice` runs here |

All of them are in [configuration.md](configuration.md), under "Local and self-hosted models".
