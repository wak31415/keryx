# Jarvis voice agent — implementation plan

> **Historical record.** This is the plan the first build followed, kept as it was;
> it is superseded wherever the spec or the code says otherwise.

Spec (binding authority): `docs/superpowers/specs/2026-08-18-jarvis-voice-agent-design.md`.
Read §3.2 "Binding interfaces" and §4 "Verified API notes" of the spec for every task —
the interfaces there are shared between tasks and must be matched exactly.

## Global Constraints

- Python 3.12, `uv`, `src/` layout (`src/jarvis/`), package name `jarvis`, CLI entry point `jarvis`.
- Tests: `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`), under `tests/`. `uv run pytest -q` must
  pass with **pristine output** (no warnings) at the end of every task. TDD: write the failing test
  first, then the implementation; report RED/GREEN evidence.
- Lint: `uv run ruff check src tests` clean at the end of every task (ruff config in pyproject: line-length 100,
  select `E,F,I,UP,B`).
- No network or hardware access in tests: OpenAI, Twilio, sounddevice, openwakeword and the Agent SDK are
  always behind an injectable interface with a fake/test double. Tests must not import `sounddevice` or
  `openwakeword` at module import time (guard heavy imports inside functions/classes so tests run without a mic).
- Audio formats: phone path is `audio/pcmu` (µ-law 8 kHz, passed straight through, no transcoding);
  local path is `audio/pcm` (16-bit LE mono 24 kHz). Never transcode on the phone path.
- Only one active OpenAI response at a time — every `response.create` goes through the provider's response queue.
- Speakable IDs: task ids are small integers (SQLite autoincrement), sessions use short random ids.
- Secrets never logged. PIN compared with `hmac.compare_digest`. PIN digits never enter the model transcript on the DTMF path.
- Each task ends with one or more commits on `main` with a conventional message (`feat: …`, `test: …`, `chore: …`).
- Data lives under `settings.data_dir` (`~/.jarvis` by default); tests use `tmp_path`.
- Logging via stdlib `logging` (`logging.getLogger("jarvis.<module>")`).

---

## Task 1: Project bootstrap, config, CLI skeleton

**Goal:** an installable `jarvis` package with settings, an empty CLI, passing test suite, README/CLAUDE.md, `.env.example`.

Files:
- `pyproject.toml` — `[project] name="jarvis" version="0.1.0" requires-python=">=3.12,<3.13"`; deps: `fastapi`, `uvicorn[standard]`,
  `websockets`, `twilio`, `pydantic-settings`, `numpy`, `sounddevice`, `soxr`, `openwakeword`, `onnxruntime`,
  `claude-agent-sdk`, `httpx`, `typer`; dev group: `pytest`, `pytest-asyncio`, `ruff`. `[project.scripts] jarvis = "jarvis.cli:app"`.
  Build backend `hatchling` (with `[tool.hatch.build.targets.wheel] packages = ["src/jarvis"]`) or `uv_build`.
  `[tool.pytest.ini_options] asyncio_mode="auto"  testpaths=["tests"]  filterwarnings=["error"]` (treat warnings as errors —
  if a third-party import emits a DeprecationWarning you cannot avoid, add a targeted `ignore::…:module` entry, not a blanket ignore).
  `[tool.ruff] line-length=100`; `[tool.ruff.lint] select=["E","F","I","UP","B"]`.
- `.python-version` → `3.12`.
- `src/jarvis/__init__.py` (`__version__`), `src/jarvis/py.typed`.
- `src/jarvis/config.py` — `Settings(BaseSettings)` with every field from spec §3.4 (exact env names, defaults).
  `model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)`; fields whose env name differs from
  the field name (`JARVIS_PIN`→`pin`) use `Field(validation_alias=...)`. `allowed_callers` parses a comma-separated string
  (use `NoDecode` + a `field_validator` in `mode="before"`, so `ALLOWED_CALLERS=+1555..,+1556..` works). `projects` parses JSON.
  Paths (`data_dir`, `projects_root`) expand `~`. Property `owner_number` → explicit `OWNER_NUMBER` else first allowed caller
  else None. Method `ensure_dirs()` creates `data_dir`, `data_dir/tasks`, `data_dir/calls`. Method `report_secret_value()`
  returns `report_secret` if set else reads/creates `data_dir/report_secret` (32 random hex bytes, mode 0600).
  Function `load_settings(**overrides) -> Settings`.
- `src/jarvis/cli.py` — typer app with `download-models` (calls `openwakeword.utils.download_models(model_names=[settings.wakeword_model])`,
  import inside the command) and a placeholder `serve` that prints "not implemented yet" (later tasks fill it in). `jarvis --help` works.
- `.env.example` documenting every env var from spec §3.4 with comments.
- `README.md` skeleton (what it is, prerequisites, quick start `uv sync`, `cp .env.example .env`, `uv run jarvis download-models`, `uv run jarvis serve`), and
  `CLAUDE.md` (how to run tests/lint, layout, the "fakes not network" testing rule, pointer to the spec).
- `tests/conftest.py` — fixture `settings(tmp_path)` returning `Settings` built with `_env_file=None`, dummy keys, `data_dir=tmp_path/"jarvis"`.
- `tests/test_config.py` — env parsing of `ALLOWED_CALLERS`, `PROJECTS` JSON, `JARVIS_PIN` alias, `owner_number` fallback,
  `report_secret_value()` persistence + 0600 mode, defaults from the spec table (spot-check 6+ fields).

Verification: `uv sync`, `uv run pytest -q` (all green, no warnings), `uv run ruff check src tests`, `uv run jarvis --help`.
Commit: `chore: bootstrap jarvis package, settings, CLI skeleton`.

---

## Task 2: EventBus + audio utilities

**Goal:** the two small shared modules everything else builds on.

Files:
- `src/jarvis/events.py` — `EventBus` and the event dataclasses exactly as spec §3.2. `subscribe` returns an unsubscribe callable;
  handlers may be sync or async; `publish` awaits async handlers sequentially, catches + logs exceptions (never propagates),
  dispatches by exact type match (`type(event)`) and also to subscribers of `object` (wildcard) — used for the transcript log.
- `src/jarvis/audio/__init__.py`, `src/jarvis/audio/util.py`:
  - `resample_pcm16(data: bytes, src_rate: int, dst_rate: int) -> bytes` via `soxr.resample` on int16 ndarray (returns int16 bytes; passthrough when rates equal).
  - `pcm16_to_mulaw(data: bytes) -> bytes` and `mulaw_to_pcm16(data: bytes) -> bytes` — pure numpy G.711 µ-law (bias 0x84, clip 32635); used only by tests / `loopback` dev mode.
  - `chunk_bytes(data: bytes, size: int) -> Iterator[bytes]`.
  - `ms_for_bytes(n: int, fmt: AudioFormat) -> float` (pcmu: 8 bytes/ms; pcm: 48 bytes/ms).
  - `class AudioGate` — half-duplex state machine, pure logic, monotonic-clock injectable (`now: Callable[[], float]`):
    states `LISTENING`/`SPEAKING`; `on_playback_start()`, `on_playback_drain()` (starts hangover), `should_pass_mic() -> bool`
    (False while SPEAKING and for `hangover_s` (default 0.2) after drain), `is_speaking` property.
  - `class PlaybackBuffer` — thread-safe byte FIFO for the speaker callback: `write(bytes)`, `read(n) -> bytes` (pads with zeros
    when short and reports underrun via return of fewer real bytes / a `drained` flag), `clear()`, `pending_bytes`, `is_empty`.
- Tests `tests/test_events.py`, `tests/test_audio_util.py`: sync+async handlers, unsubscribe, exception isolation, wildcard;
  µ-law round trip on a sine (error bounded, e.g. max abs error < 3% of full scale), resample length ratio + passthrough,
  chunking, ms_for_bytes, AudioGate transitions with a fake clock, PlaybackBuffer read/pad/clear.

Commit: `feat: event bus and audio utilities`.

---

## Task 3: OpenAI Realtime provider

**Goal:** `realtime/base.py` (protocol + events + `SessionConfig`, spec §3.2) and `realtime/openai.py` `OpenAIRealtimeClient`
implementing the GA schema from spec §4, fully unit-tested against a fake websocket.

Requirements:
- `OpenAIRealtimeClient(api_key, model, *, ws_connect=None)` — `ws_connect` is an injectable async factory
  `(url, headers) -> ws` (default: `websockets.connect(url, additional_headers=…, max_size=None)`); the ws object needs `send(str)`,
  `recv() -> str`, `close()`. Tests pass a `FakeWS` with scripted incoming JSON events and a list of sent messages.
- `connect(config)`: open WS to `wss://api.openai.com/v1/realtime?model=<model>` with `Authorization: Bearer <key>` (no beta header),
  send `session.update` built exactly per spec §4 from `SessionConfig` (format for both input and output, `server_vad` block with
  `create_response: true` and `interrupt_response` from config, `transcription` block only when `transcription_model` set,
  tools, `tool_choice: "auto"`, instructions), start the reader task.
- `events()` async iterator over typed events, translated from server events: `response.output_audio.delta` → `AudioDelta`
  (base64-decoded); `input_audio_buffer.speech_started` → `SpeechStarted`; `speech_stopped` → `SpeechStopped`;
  `response.created` → `ResponseStarted`; `response.done` → `ResponseDone` (also drains the response queue: send the next queued
  `response.create` if any); `response.function_call_arguments.done` → `FunctionCall` (arguments JSON-parsed; invalid JSON → `{}`
  + logged); `response.output_audio_transcript.done` → `Transcript(role="assistant")`;
  `conversation.item.input_audio_transcription.completed` → `Transcript(role="user")`; `error` → `ProviderError`
  (fatal only for auth/connection-class errors; a `conversation.item.truncate` "already shorter" style error is non-fatal);
  reader exit (WS closed / exception) → `Disconnected(reason)` then the iterator ends. Unknown events are ignored (debug log).
- `send_audio(bytes)` → `input_audio_buffer.append{audio: b64}`.
- `submit_tool_result(call_id, output)` → `conversation.item.create{item:{type:"function_call_output",call_id,output:<json str>}}`
  then `_request_response()`.
- `inject_message(text, respond=True, response_instructions=None)` → `conversation.item.create` with a system message
  (`content:[{type:"input_text",text}]`), then if `respond` → `_request_response(instructions)`.
- `_request_response(instructions=None)`: builds `response.create` (`{"response":{"instructions":…}}` only when given);
  if a response is active → append to `_pending_responses` deque; else send and mark active. Active flag set on
  `response.created`, cleared on `response.done`; also treat send of `response.create` as active until `response.created`
  arrives (so two quick injects don't both send).
- `truncate(item_id, audio_end_ms)` → `conversation.item.truncate{item_id,content_index:0,audio_end_ms}`; `cancel_response()` → `response.cancel`.
- `reconnect()`: close old WS, reset state (`active`, queue), re-open + re-send `session.update`; returns True/False; at most one attempt per call.
- `close()`: cancel reader, close WS, make `events()` finish.
- Also expose `provider.session_id`/`last_error` for debugging (optional).

Tests `tests/realtime/test_openai_client.py` (+ `tests/realtime/fake_ws.py`): session.update payload shape (deep-equal against a
fixture dict for pcmu and pcm configs, including `interrupt_response=False`); each server-event → typed event mapping;
response queue behaviour (two `inject_message` calls while a response is active → exactly one `response.create` sent until
`response.done`, then the second); tool result flow; truncate; Disconnected on ws close; reconnect re-sends session.update.
Add fixture JSON files under `tests/fixtures/realtime/` for the server events used.

Commit: `feat: OpenAI Realtime provider (GA schema) with fake-ws tests`.

---

## Task 4: Transport protocol, local audio device, wake word

**Goal:** `transports/base.py`, `transports/local_audio.py`, `wakeword.py`. Hardware behind thin adapters; all logic testable.

- `src/jarvis/transports/__init__.py`, `transports/base.py` — exactly spec §3.2 (`AudioIn`, `Dtmf`, `Hangup`, `Transport` protocol,
  `AudioFormat`).
- `transports/local_audio.py`:
  - `class LocalAudioDevice`: owns mic + speaker. Constructor takes `sample_rate=24000`, `frame_samples=1920`, `gate: AudioGate`,
    `stream_factory` (injectable; default builds `sounddevice.InputStream`/`RawOutputStream` — import `sounddevice` lazily inside
    the default factory). `start()`/`stop()`. Mic frames (int16 bytes @24 kHz) are routed via `set_mic_sink(callable | None)`:
    when a sink is set and `gate.should_pass_mic()`, the frame is delivered (thread-safe hand-off to the asyncio loop with
    `loop.call_soon_threadsafe`); a second sink `set_wake_sink(callable | None)` always receives the 16 kHz resampled copy
    (1280 samples) regardless of the gate (so the wake word can also be used while idle). Speaker: `play(bytes)` writes to a
    `PlaybackBuffer`; the output callback reads from it, calls `gate.on_playback_start()` on first non-empty read and
    `gate.on_playback_drain()` when it runs empty; `clear_playback()`. `chime()` plays a short generated two-tone (numpy sine,
    ~250 ms). Expose `is_playing`.
    Provide a `FakeStreamFactory` in `tests/` (not in src) that lets tests push mic frames and pull speaker output by invoking the
    callbacks directly.
  - `class LocalTransport(Transport)`: `channel="local"`, `caller=None`, `audio_format="audio/pcm"`. Constructed with a device;
    `events()` yields `AudioIn` from the device mic sink until `hangup()`; `send_audio` → `device.play`; `clear` → `device.clear_playback`;
    `hangup` → unset sink, finish iterator (emit `Hangup(reason)`).
- `wakeword.py`:
  - `class WakeWordDetector(Protocol)`: `sample_rate: int` (16000), `frame_samples: int` (1280), `def score(self, frame_int16: bytes) -> float`, `def reset(self) -> None`.
  - `class OpenWakeWordDetector(model_name="hey_jarvis")`: lazy import of `openwakeword`, `Model(wakeword_models=[name], inference_framework="onnx")`,
    `score()` → `max(model.predict(np.frombuffer(frame, np.int16)).values())`, `reset()` → `model.reset()`.
  - `class WakeWordListener(detector, threshold=0.5, refractory_s=2.0, now=time.monotonic)`: `feed(frame) -> bool` returns True on a
    detection (score ≥ threshold and outside the refractory window); `on_wake` callback optional.
  - `PorcupineDetector` is optional; skip unless trivial (leave a docstring note).
- Tests: `tests/transports/test_local_audio.py` (fake stream factory: mic frames reach the mic sink only when gate allows; wake sink
  gets 1280-sample 16 kHz frames; play/clear/drain toggles the gate; LocalTransport yields AudioIn and finishes on hangup),
  `tests/test_wakeword.py` (fake detector: threshold + refractory).

Commit: `feat: transport protocol, local audio device, wake-word listener`.

---

## Task 5: VoiceSession core, tool registry, prompt, local runner, CLI serve/loopback

**Goal:** the transport-agnostic session that ties transport ⇄ provider, plus the tool registry and the local wake-word loop, so
`jarvis serve --no-phone` gives a working "hey jarvis" assistant (no tasks yet).

- `src/jarvis/tools/__init__.py`, `tools/registry.py` — `ToolRegistry`, `ToolContext`, `ToolHandler` per spec §3.2. `call()` validates the
  name, runs the handler, catches exceptions → `{"error": str}`; result must be JSON-serialisable dict.
- `src/jarvis/prompts/voice_system.md` — the receptionist persona: named Jarvis, concise spoken style (1–2 sentences unless asked),
  never read code/URLs/ids letter by letter, confirm before dispatching a task (repeat the gist), say "one moment" before a
  tool call that may take a while, project-name mapping guidance, PIN handling guidance (ask the caller to say or key in the PIN
  when a tool returns `pin_required`), announce task results briefly when told via `[system]` messages, how to end the session.
  Template placeholders: `{now}`, `{channel}`, `{caller}`, `{authorized}`, `{projects}`, `{opening_context}`. Load with
  `importlib.resources`. Include the `prompts/` directory in the wheel.
- `src/jarvis/session.py`:
  - `VoiceSession` and `SessionRegistry` per spec §3.2. `run()`:
    1. build `SessionConfig` (format = transport.audio_format; `interrupt_response = channel == "phone"`; voice/model from settings; tools = registry.schemas(); instructions rendered from the prompt template),
    2. `provider.connect(config)`, publish `SessionStarted`, register in the registry (registry passed via constructor kw `registry: SessionRegistry | None`),
    3. opening: `inject_message("[session opened] Greet the user briefly." or the opening_context, respond=True)`,
    4. run two pumps concurrently (`asyncio.TaskGroup`): transport→provider (`AudioIn` → `send_audio`; `Dtmf` → PIN buffer (Task 10 fills in the logic — leave a hook `_on_dtmf(digit)`); `Hangup` → end) and provider→transport
       (`AudioDelta` → track current item id, first-delta timestamp (transport clock from last `AudioIn.timestamp_ms` if available else wall clock), bytes sent per item, then `transport.send_audio`;
       `SpeechStarted` → if phone: `transport.clear()` + `provider.truncate(current_item, played_ms)` (played_ms = clock delta capped by `ms_for_bytes(sent)`), reset item tracking; also resets the silence timer;
       `FunctionCall` → spawn a task: `registry.call(name, args, ctx)` → `provider.submit_tool_result(call_id, result)`;
       `Transcript` → append to `data_dir/calls/<session_id>.log` (`[HH:MM:SS] user: …` / `assistant: …`);
       `ResponseDone` → arm the local silence timer; `ProviderError(fatal)` → end; `Disconnected` → `provider.reconnect()`; on success inject "[system] The connection was reset; briefly apologize and continue." else end),
    5. silence timeout (local only): if no user speech within `settings.local_silence_timeout` after a `ResponseDone` → `inject_message("[system] The user has been silent; say a brief goodbye.")` and end when that response is done,
    6. `max_call_seconds` (phone): inject a wrap-up message at T-30 s, end at T,
    7. `request_end(reason)`: sets a flag; the loop finishes the current response (bounded by 10 s), then `transport.hangup()`, `provider.close()`, publish `SessionEnded`, unregister.
  - `announce(text) -> bool`: if live → `inject_message(f"[system] {text}", respond=True, response_instructions="Briefly tell the user about this in one or two sentences.")` and return True.
  - `authorize()` sets `authorized=True`.
- `src/jarvis/local_runner.py` — `LocalRunner(settings, device, detector, session_factory, registry)`: `run()` loop: set wake sink → on detection: chime, build a `VoiceSession(LocalTransport(device), provider_factory(), …, authorized=True)`, `await session.run()`, then back to listening (call `detector.reset()`); handles cancellation cleanly. `session_factory`/`provider_factory` injectable.
- `src/jarvis/cli.py`: `serve --no-phone` builds settings, `OpenAIRealtimeClient` factory, `LocalAudioDevice`, `OpenWakeWordDetector`, `ToolRegistry` (empty for now, Task 10 fills it) and runs `LocalRunner`. `--no-wakeword` skips the local runner (phone server comes in Task 6). Add `loopback --wav in.wav [--out out.wav]`: reads a 16-bit mono WAV (any rate; resample to 24 kHz), builds a `WavTransport` (in `cli.py` or `transports/wav.py`: yields the WAV as 20 ms `AudioIn` frames in real time, then silence for `--tail-seconds` (default 8), collects `send_audio` bytes) and a `VoiceSession` with the real provider; writes the reply to `out.wav`; prints the transcript. This is the dev harness for the provider without a mic.
- Tests: `tests/test_registry.py`; `tests/test_session.py` with `FakeProvider` (scriptable events queue + records of calls) and `FakeTransport` (scriptable transport events + captured audio) in `tests/fakes.py`: audio pump both ways; barge-in on phone (clear + truncate with capped ms) and no barge-in on local; function call → registry → `submit_tool_result` payload; transcript log written; silence timeout ends a local session (use small timeout); Hangup ends session and publishes SessionEnded; announce() injects; Disconnected → reconnect path. `tests/test_local_runner.py` with fake device/detector/session.

Manual (not automated): `uv run jarvis serve --no-phone` → say "hey jarvis, what time is it" → spoken reply.
Commit(s): `feat: voice session core and tool registry`, `feat: local wake-word runner and CLI serve/loopback`.

---

## Task 6: Twilio transport, FastAPI server, auth

**Goal:** phone path end-to-end (chat only): `POST /twilio/voice` → TwiML → `WS /twilio/media` → `VoiceSession(TwilioTransport)`.

- `src/jarvis/transports/twilio_ws.py` — `TwilioTransport(ws)` where `ws` is any object with `async receive_text()`, `async send_text(str)`,
  `async close(code=1000)` (Starlette `WebSocket` satisfies this; tests use a fake). `channel="phone"`, `audio_format="audio/pcmu"`.
  `await transport.start(timeout=10) -> StartInfo(stream_sid, call_sid, custom_parameters: dict)` consumes messages until `start`
  (ignoring `connected`); sets `caller` from `customParameters["caller"]`. `events()`: `media` → `AudioIn(base64-decoded, timestamp_ms=int(media.timestamp))`;
  `dtmf` → `Dtmf(digit)`; `stop` / socket closed → `Hangup("stop")`/`Hangup("disconnect")` then finish. `send_audio(data)` → `{"event":"media","streamSid":…,"media":{"payload":b64}}`;
  `clear()` → `{"event":"clear","streamSid":…}`; `hangup()` → close ws (idempotent). Also `send_mark(name)` (used by tests / future).
- `src/jarvis/server.py` — `create_app(state: AppState) -> FastAPI`:
  - `POST /twilio/voice` (form): validate `X-Twilio-Signature` with `RequestValidator(settings.twilio_auth_token)` against the URL rebuilt
    from `x-forwarded-proto`/`x-forwarded-host` (fallback `request.url`) and the form params → 403 on failure; `From` must be in
    `settings.allowed_callers` → else TwiML `<Response><Say>Sorry, this number is private.</Say><Hangup/></Response>`;
    otherwise mint a token via `state.stream_tokens.issue(caller=From, extra={...})` and return TwiML
    `<Response><Connect><Stream url="wss://{public_host}/twilio/media"><Parameter name="token" value="…"/><Parameter name="caller" value="…"/></Stream></Connect></Response>` (`Content-Type: text/xml`).
    Use `twilio.twiml.voice_response.VoiceResponse` to build it.
  - `WS /twilio/media`: accept, `TwilioTransport(ws).start()`, look up + consume the token (`state.stream_tokens.redeem(token) -> TokenInfo | None`; unknown/expired → close 1008);
    build `VoiceSession(transport, state.provider_factory(), settings, state.registry, state.bus, authorized=False, registry=state.sessions, opening_context=<from token extra, e.g. task summary>)` and `await session.run()`.
  - `POST /twilio/status`: signature-validate, log `CallSid`/`CallStatus`, 204.
  - `GET /health` → `{"ok": true, "live_sessions": n}`.
  - Signature validation is skipped when `settings.twilio_auth_token` is None **and** `settings.debug_skip_twilio_validation` is True (add that setting, default False) — otherwise 403.
- `src/jarvis/app.py` — `AppState` dataclass: `settings`, `bus`, `sessions: SessionRegistry`, `registry: ToolRegistry`, `provider_factory: Callable[[], RealtimeProvider]`,
  `stream_tokens: StreamTokenStore` (in-memory: `issue(caller, extra) -> str` (secrets.token_urlsafe), `redeem(token)`, TTL 60 s, single use);
  `build_app_state(settings) -> AppState` wiring the real provider factory. Task 9–11 extend it with store/manager/notifier — leave optional fields with `None` defaults.
- `cli.py serve`: unless `--no-phone`, run uvicorn programmatically (`uvicorn.Server(uvicorn.Config(app, host, port, log_level))` → `await server.serve()`) concurrently with the local runner (unless `--no-wakeword`); Ctrl-C stops both.
- `scripts/dev.sh` — starts `ngrok http --domain=$PUBLIC_HOST $PORT` in the background and `uv run jarvis serve --no-wakeword` in the foreground (reads `.env`).
- Tests: `tests/transports/test_twilio_ws.py` (fake ws: start parsing incl. customParameters, media→AudioIn with timestamp, dtmf, stop→Hangup, send_audio/clear payloads, hangup idempotent);
  `tests/test_server.py` with `httpx.AsyncClient`/`fastapi.testclient`: valid signature + allowed caller → TwiML containing `<Stream url="wss://host/twilio/media">` and a `token` Parameter; bad signature → 403; disallowed caller → Say+Hangup TwiML;
  websocket route with `TestClient.websocket_connect` + a fake provider: unknown token → close; valid token → session runs (use a fake provider factory that ends immediately or emits one AudioDelta and check a `media` message comes back); `/health`.
  Compute a real signature in tests with `RequestValidator.compute_signature`.

Manual: `scripts/dev.sh`, set the Twilio number's voice webhook to `https://<PUBLIC_HOST>/twilio/voice`, call from phone; check barge-in.
Commit(s): `feat: Twilio media-stream transport`, `feat: FastAPI server with Twilio auth and stream tokens`.

---

## Task 7: Task models and SQLite store

- `src/jarvis/tasks/__init__.py`, `tasks/models.py` — `Task`, `TaskKind`, `TaskStatus`, `DESTRUCTIVE_KINDS` per spec §3.2, plus
  `Task.to_row()/from_row()` helpers and `Task.short_status_line()` (speakable one-liner: "task 3 (coding, running): add README to garmin-voice-agent").
- `tasks/store.py` — `TaskStore` per spec §3.2 using stdlib `sqlite3` (WAL mode, `check_same_thread=False`, one connection + `threading.Lock`,
  sync work executed via `asyncio.to_thread`); schema with `schema_version` table and a `_migrate()` that creates v1; `id INTEGER PRIMARY KEY AUTOINCREMENT`;
  timestamps stored ISO-8601 UTC; enums stored as their string values.
- Tests `tests/tasks/test_store.py`: create/get round-trip of every field, update partial fields, list ordering + status filter + limit,
  `count_created_since`, works with `tmp_path` file and `":memory:"`, `close()`.

Commit: `feat: task models and SQLite store`.

---

## Task 8: Agent runner (Claude Agent SDK) + subagent prompt

- `src/jarvis/prompts/subagent_suffix.md` — appended to the Claude Code preset system prompt: you are a subagent dispatched by voice; work fully autonomously,
  never ask questions; be thorough; when done, end your final message with a `SPOKEN_SUMMARY:` line followed by 1–3 short spoken-style
  sentences (no code, no URLs, no markdown) summarising the outcome; the full report goes above it. `{kind}`, `{project}`, `{description}` placeholders optional.
- `src/jarvis/tasks/agent_runner.py`:
  - `RunResult`, `AgentSession`, `AgentRunner` protocols per spec §3.2; `extract_spoken_summary(text)`.
  - `build_options(task, settings, *, resume=None) -> ClaudeAgentOptions` (pure, unit-testable): `permission_mode="bypassPermissions"`, `cwd` (task.cwd or `settings.data_dir/"workspace"`, created), `setting_sources=["user","project"]`,
    `system_prompt={"type":"preset","preset":"claude_code","append": rendered suffix}`, `model=resolve_model(task.model)`, `max_turns=settings.subagent_max_turns`, `max_budget_usd=settings.subagent_max_budget_usd`,
    `env={"ANTHROPIC_API_KEY": settings.anthropic_api_key}` when set, `resume=resume`. Per kind: `coding` → all tools (no `allowed_tools` restriction); `research`/`chat` → `allowed_tools=["WebSearch","WebFetch","Read","Glob","Grep"]` (+ `Write` for research so it can save a report);
    `cowork` → `mcp_servers={"google": {"type":"stdio","command":"uvx","args":["workspace-mcp","--tools","gmail","calendar","--transport","stdio","--single-user"],"env":{GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REDIRECT_URI="http://localhost:8000/oauth2callback", USER_GOOGLE_EMAIL, GOOGLE_MCP_CREDENTIALS_DIR=data_dir/google, OAUTHLIB_INSECURE_TRANSPORT="1"}}}`,
    `allowed_tools=["mcp__google__*","WebSearch","WebFetch","Read"]`.
    `resolve_model(name)`: `opus→claude-opus-5`, `sonnet→claude-sonnet-5`, `fable→claude-fable-5`, `haiku→claude-haiku-4-5-20251001`, `None/""→settings.subagent_model`, anything else passthrough.
  - `ClaudeAgentRunner(settings, *, client_factory=None)`: `open()` builds options and a `ClaudeSDKClient` (factory injectable for tests), `connect()`s, returns `ClaudeAgentSession`.
    `ClaudeAgentSession.run(prompt, on_progress)`: `client.query(prompt)`; iterate `receive_response()`; for `AssistantMessage` text blocks → `on_progress(text)`, `ToolUseBlock` → `on_progress(f"[tool] {name} {short input}")`; on `ResultMessage` capture `session_id`, `result`, `total_cost_usd`, `is_error`; return `RunResult` (`spoken_summary=extract_spoken_summary(final_text)`; ok = not is_error). Exceptions → `RunResult(ok=False, error=str(e))`.
    `send(text)` → `client.query(text)`; `interrupt()` → `client.interrupt()`; `close()` → `client.disconnect()` (ignore errors).
  - `FakeAgentRunner(script: dict[str|None, list[FakeStep]] | callable)`: sessions whose `run()` sleeps `delay_s`, emits given progress lines, returns a scripted `RunResult`; records `send()` texts and `interrupt()` calls; supports `resume` (records the resume id). Lives in `src/jarvis/tasks/agent_runner.py` (it is used by the CLI `--fake-agents` dev flag later, so it is not test-only).
- Tests `tests/tasks/test_agent_runner.py`: `extract_spoken_summary` (block present / absent / trailing whitespace / multi-line); `build_options` per kind + model resolution + resume + cowork MCP env; `ClaudeAgentSession.run` with a fake SDK client (yields AssistantMessage/ResultMessage objects — construct real `claude_agent_sdk.types` dataclasses) → RunResult fields, progress callback calls, error path.

Commit: `feat: Claude Agent SDK runner with fake runner and spoken-summary extraction`.

---

## Task 9: TaskManager

- `src/jarvis/tasks/manager.py` — `TaskManager` per spec §3.2:
  - `dispatch()`: enforce `daily_task_cap` (`store.count_created_since(midnight UTC today)` → `TaskLimitError`), resolve `project` (`resolve_project`: exact name in `settings.projects`, else case/`-`/`_`/space-insensitive match against `settings.projects` keys and immediate subdirectories of `settings.projects_root`; `UnknownProjectError` listing candidates), set `cwd`, `model=resolve_model(model)`, create the row (`queued`), publish nothing yet, schedule `_run(task_id)` via `asyncio.create_task` (kept in a set), return the task.
  - `_run`: `async with semaphore` → status `running` (+`started_at`), publish `TaskStarted`, open agent session (`runner.open(task)`), build the prompt (kind-specific preamble + description; coding includes "Repository: <cwd>"), `run(prompt, on_progress)`; progress → append line to `data_dir/tasks/<id>.log` and publish `TaskProgress`; result → write `data_dir/tasks/<id>.md` (final_text), update row (`done`/`failed`, `summary`, `report_path`, `claude_session_id`, `error`, `finished_at`), close session, publish `TaskCompleted(id, summary)` or `TaskFailed(id, error)`. Cancellation (`asyncio.CancelledError` from `cancel()`) → `interrupt()`, status `cancelled`, `TaskFailed(id, "cancelled")` is **not** published (publish nothing), close.
  - `wait_for(task_id, timeout)`: per-task `asyncio.Event` set on terminal state; returns latest row.
  - `followup(task_id, text)`: running → `live_session.send(text)` and append to log; done/failed → new run with `runner.open(task, resume=claude_session_id)`, status back to `running`, same event flow (summary overwritten); queued → append text to the description before it starts; cancelled → error.
  - `cancel(task_id)`: running → cancel the asyncio task; queued → mark `cancelled` (skip when its turn comes).
  - `list_projects()`: configured projects first, then `projects_root` subdirs (sorted, hidden dirs excluded).
  - `shutdown()`: cancel running asyncio tasks, close sessions.
- Tests `tests/tasks/test_manager.py` with `FakeAgentRunner`, in-memory store, `EventBus` recorder: dispatch→done with events + files written; semaphore (max 1 → second stays queued until first finishes); wait_for timeout vs completion; followup on running (send recorded) and on done (resume id recorded); cancel running (interrupt recorded, status cancelled) and queued; daily cap; project resolution (configured, fuzzy, unknown); prompt content per kind.

Commit: `feat: task manager with queueing, follow-ups, cancel and events`.

---

## Task 10: Voice tools + session wiring (dispatch, PIN gate, announcements)

- `src/jarvis/tools/builtin.py` — `register_builtin_tools(registry, *, manager: TaskManager, settings, notifier=None)` registering with schemas (clear descriptions written for the voice model):
  - `dispatch_task(kind, description, project?, model?, wait_seconds?)` → PIN check (`kind in DESTRUCTIVE_KINDS and ctx.channel=="phone" and not ctx.authorized` → `{"status":"pin_required","message":"…"}`; if `settings.pin` is None on phone → `{"status":"refused","message":"PIN not configured"}`), then `manager.dispatch(...)`, `wait_for(id, min(wait_seconds or 0, settings.dispatch_wait_max_seconds))` → `{"task_id", "status", "summary"?}`; `UnknownProjectError` → `{"error":..., "candidates":[…]}`; `TaskLimitError` → error.
  - `list_tasks(status?: "running"|"done"|"failed"|"all", limit?)` → `{"tasks":[{id, kind, status, description(≤120 chars), summary?}]}`.
  - `get_task_status(task_id)`, `get_task_result(task_id)` (summary + first ~1500 chars of report), `send_followup(task_id, message)`, `cancel_task(task_id)`,
  - `list_projects()` → `{"projects":[names]}`,
  - `request_callback(task_id, number?)` → sets `callback_requested=True`, `callback_number = number or ctx.caller or settings.owner_number` (error if none),
  - `submit_pin(pin)` → `ctx.session.submit_pin(pin)` result (`{"status":"authorized"}` / `{"status":"invalid","attempts_left":n}` / `{"status":"locked"}`),
  - `end_session()` → `ctx.session.request_end("user")` → `{"status":"ending"}`.
- `src/jarvis/session.py` additions: `submit_pin(pin) -> dict` (constant-time compare with `settings.pin`; on success `authorize()`; count attempts; on 3rd failure `inject_message("[system] Too many failed PIN attempts. Say goodbye.")` + `request_end("pin_lockout")`); DTMF path: `_on_dtmf(digit)` collects digits into a buffer with a 5 s inter-digit reset; on `#` or when the buffer length equals `len(settings.pin)` → `submit_pin(buffer)`; on success `inject_message("[system] The caller entered the correct PIN and is now authorized for all tasks. Acknowledge briefly.")`, on failure inject a brief "[system] PIN incorrect, ask them to try again" — digits never appear in any message to the model. Ignore DTMF when no PIN is configured. When a session is authorized, `ToolContext.authorized` reflects it live.
- `src/jarvis/app.py`: `AppState` gains `store`, `manager`; `build_app_state()` creates `TaskStore(data_dir/"tasks.db")`, the runner (`ClaudeAgentRunner`, or `FakeAgentRunner` when `settings.fake_agents` — add that boolean setting, env `FAKE_AGENTS`, default False), `TaskManager`, and calls `register_builtin_tools`. `cli.py serve` uses it for both local and phone; add `--fake-agents` flag → sets the setting.
- Task-completion announcement in live sessions is done by the Notifier in Task 11; for this task, wire a minimal `bus.subscribe(TaskCompleted, …)` in `AppState` that calls `announce()` on every live session (moved into the Notifier in Task 11).
- Tests `tests/tools/test_builtin.py` (fake manager or real manager + FakeAgentRunner): each tool's happy path + PIN gating matrix (local always allowed; phone unauthorized coding → pin_required; phone authorized → dispatched; phone chat kind → allowed without PIN); `tests/test_session_pin.py`: spoken submit_pin success/failure/lockout ends session; DTMF collection with `#` and with exact length; digits absent from provider `inject_message` texts; `tests/test_announce.py`: TaskCompleted on the bus → live sessions' provider receives an inject.

Commit(s): `feat: voice tools for tasks, projects and PIN`, `feat: PIN gate with DTMF and spoken entry`.

---

## Task 11: Notifications, reports, call-backs

- `src/jarvis/notify/__init__.py`, `notify/twilio_out.py` — `TwilioOut(settings, client=None)` (client injectable; default `twilio.rest.Client`; calls run in `asyncio.to_thread`): `send_sms(to, body) -> str (sid)`, `place_call(to, *, twiml: str, status_callback: str|None) -> str`, `stream_twiml(public_host, params: dict[str,str]) -> str` (VoiceResponse with `<Connect><Stream>` + `<Parameter>` per param).
- `notify/notifier.py` — `Notifier(bus, store, sessions, twilio_out, settings, stream_tokens)`: `start()` subscribes to `TaskCompleted`/`TaskFailed`. On completion: text = `f"Task {id} ({kind}) finished: {summary}"` (or failed …); (1) for each live session `await s.announce(text)`; if any phone session announced → mark `announced=True`; (2) if not announced-by-phone and Twilio configured → SMS to `task.origin_caller` if origin phone else `settings.owner_number`; body = summary (≤ 1200 chars) + report link `https://{public_host}/reports/{id}?t={token}`; set `sms_sent`; (3) if `task.callback_requested` and not announced-by-phone → issue a stream token with `extra={"task_id": id, "opening_context": text}` and `place_call(callback_number, twiml=stream_twiml(host, {"token":…, "caller": number, "task_id": id}))`. All Twilio errors are logged, never raised. `report_token(task_id) -> str` = `hmac.new(secret, str(id).encode(), sha256).hexdigest()[:32]`; `verify_report_token`.
- `server.py`: `GET /reports/{task_id}?t=…` → verify token (compare_digest) → serve `report_path` as `text/markdown; charset=utf-8` (404 if missing, 403 if bad token). WS route: when the token's `extra` has `opening_context`, pass it to the session (`opening_context` → prompt: "You are calling the user back about a finished task: …; greet and tell them the result, then ask if they need anything else").
- `app.py`: build `TwilioOut` (only when Twilio settings are present) and `Notifier`; remove the interim announce subscription from Task 10.
- Tests `tests/notify/test_twilio_out.py` (fake client records `messages.create`/`calls.create` kwargs; TwiML contains parameters), `tests/notify/test_notifier.py` (live phone session announced → no SMS; no live session → SMS to origin caller with report link; local-origin task → SMS to owner; callback requested → `calls.create` with twiml containing token + task_id and a token registered in `stream_tokens`; failed task → SMS mentions failure; Twilio exception swallowed), `tests/test_reports.py` (valid token → markdown, bad token → 403, missing → 404).

Manual: hang up mid-task → SMS arrives; call back → "what's the status of my last task"; "call me back when it's done".
Commit: `feat: task notifications via live session, SMS and call-back; tokenized reports`.

---

## Task 12: CLI completeness, ops, guardrails, docs

- `cli.py`: `tasks list [--status] [--limit]` (table: id, status, kind, created, description), `tasks show ID` (all fields + summary + report path),
  `setup-google` (runs `uvx workspace-mcp --tools gmail calendar --transport stdio --single-user` once with the env from `build_options` so the browser OAuth flow completes;
  prints instructions; requires `GOOGLE_OAUTH_CLIENT_ID/SECRET`), `doctor` (checks: `.env` present, `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` set, `claude` CLI on PATH, Twilio settings + `PUBLIC_HOST`, `ngrok` on PATH,
  wake-word model files present, mic device available (try `sounddevice.query_devices()` in a try/except), data dir writable; prints ✅/❌ lines and exits non-zero on failures), `serve` flags finalised (`--no-phone`, `--no-wakeword`, `--fake-agents`, `--host`, `--port`).
- `ops/launchd/com.william.jarvis.plist` and `ops/launchd/com.william.ngrok.plist` (templates with `__REPO__`/`__HOME__` placeholders; `RunAtLoad`, `KeepAlive`, logs to `~/.jarvis/logs/`), `scripts/install-launchd.sh` (renders + `launchctl bootstrap gui/$UID`).
  Log rotation: `RotatingFileHandler` for `~/.jarvis/logs/jarvis.log` (10 MB × 5) configured in `serve`.
- Guardrails already in code (`max_call_seconds`, `subagent_max_turns/budget`, `daily_task_cap`) — verify each is wired and documented; add tests where missing (max_call_seconds wrap-up in session tests).
- `README.md` full: what it is, architecture diagram, setup (env, Twilio console webhook, ngrok reserved domain, Google OAuth), running (`serve`, `dev.sh`, launchd), usage examples (phone + local phrases), security model (bypassPermissions = full user access; tunnel exposes only Twilio-signed endpoints + tokenized reports; PIN), cost notes, troubleshooting (`jarvis doctor`).
- Tests: `tests/test_cli.py` with typer `CliRunner` for `tasks list/show` (temp store), `doctor` (with a settings fixture, sounddevice import failure tolerated), `--help`.

Commit(s): `feat: tasks/doctor/setup-google CLI commands`, `chore: launchd templates, log rotation, README`.
