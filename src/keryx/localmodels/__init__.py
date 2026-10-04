"""Models on this machine: what it can hold, which to download, and the servers that run them.

`keryx setup`'s "Local models" section and `keryx models` are built from this package, the
way LM Studio or VoiceInk would put it to someone: here is your hardware (`hardware`), here
are the models that fit it, the best one marked (`catalog`), and Keryx downloads it
(`download`), installs what serves it (`runtimes`) and keeps it running as a user service of
its own (`servers`, behind `keryx models serve llm|voice`).

Two servers, both bound to 127.0.0.1:

- **llm** — llama.cpp's `llama-server` with one GGUF from the catalog. It speaks Anthropic's
  Messages API and the Responses API at once, so it is the `local` agent's server in either
  harness, and the voice server's language model.
- **voice** — Hugging Face's speech-to-speech, an OpenAI Realtime server over a cascade
  (voice activity, speech-to-text, the llm above, text-to-speech): `VOICE_BASE_URL`.

Nothing here reaches the network or starts a process except through a function handed in,
so `keryx setup` routes every one of them through its `Probes`.
"""
