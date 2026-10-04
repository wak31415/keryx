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
uv run keryx config set LLM_SERVER_MODEL qwen3-coder-30b-a3b \
    LOCAL_AGENT_BASE_URL http://127.0.0.1:8090 LOCAL_AGENT_MODEL qwen3-coder-30b-a3b \
    AGENTS_ENABLED claude,local
scripts/install-systemd.sh --llm     # install-launchd.sh on a Mac
uv run keryx auth status --smoke     # one real task on each enabled agent
```

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
| `LLM_SERVER_MODEL`, `LLM_SERVER_PORT` | the model `keryx-llm` serves here, and its port |
| `VOICE_SERVER_PORT`, `VOICE_SERVER_STT`, `VOICE_SERVER_TTS`, `VOICE_SERVER_VOICE`, `VOICE_SERVER_ARGS` | the voice server `keryx-voice` runs here |

All of them are in [configuration.md](configuration.md), under "Local and self-hosted models".
