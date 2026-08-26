# Jarvis — phone + wake-word voice agent with Claude subagents

Design spec, 2026-08-18. This is the binding authority for the implementation plan in
`docs/superpowers/plans/2026-08-18-jarvis-voice-agent-plan.md`.

## 1. Goal

A personal voice agent William can reach two ways:

1. **By phone** (Garmin watch / mobile → Twilio number). The call opens a realtime voice
   session; the agent chats, answers questions, and **dispatches powerful subagents** that
   run on the host machine with full local access (repos, files, web, email, calendar).
2. **Locally, always-on** — a "hey jarvis" wake-word listener on the machine's mic/speaker
   that opens the same kind of session without a phone.

**Hosts (amended 2026-08-24).** The two channels no longer have to live on one machine:
the phone channel runs on a Linux box that is up 24/7, and the wake-word channel stays on
macOS. openwakeword 0.6.0 requires `tflite-runtime` on Linux, whose newest release has no
cp312 wheel, so its dependencies are marked `sys_platform == 'darwin'` and a Linux host
serves with `--no-wakeword`.

Reference: `frederikb96/twilio-voice-bridge` (thin Twilio↔OpenAI-Realtime relay, no tools).
We borrow its transport/provider split and Twilio handling and add a transport-agnostic
session core, a tool layer, Claude Agent SDK subagents, a task registry, notifications,
PIN gating, and the local wake-word transport.

## 2. Decisions

| Decision | Choice |
|---|---|
| Realtime voice layer | **OpenAI Realtime API** (`gpt-realtime-2.1`, speech-to-speech, server VAD, function calling) |
| Subagent runtime | **Claude Agent SDK (Python)**, `permission_mode="bypassPermissions"`, in-process |
| Mail + calendar access | The Claude CLI's own **claude.ai connectors** (Gmail, Calendar, Drive), which every spawned CLI already carries authorized. *Amended 2026-08-24, was: a `workspace-mcp` stdio server — kept behind `GOOGLE_WORKSPACE_MCP` (default off) for a machine whose subagents authenticate with an API key and so have no connectors. Measured: the connectors answered (20 threads, 13 calendars) while workspace-mcp returned "Google Authentication Needed".* |
| Task kinds | **One** (`agent`): full tools, the machine, Gmail/Calendar, skills and subagents of its own. *Amended 2026-08-24, was: chat/research/coding/cowork with per-kind tool restrictions — classifying a request is a decision the voice model is badly placed to make, and it walled mail off from code.* |
| Voice-side answers | The voice model answers small factual questions itself via a `web_search` function tool backed by the **Responses API** (a Realtime session accepts only `function` and `mcp` tools — there is no hosted search there). Everything else is dispatched. |
| Results | Announce in live session → SMS summary → persist tasks (SQLite) → outbound call-back only when requested |
| Exposure | **Cloudflare Tunnel** (`cloudflared`, `--protocol http2`) to a routed hostname; server as a launchd agent (macOS) or a systemd user unit (Linux). *Amended 2026-08-24, was: ngrok reserved domain + launchd.* |
| Auth | Twilio signature + caller allowlist + one-time stream token; **PIN only for destructive kinds** (`coding`, `cowork`); local sessions pre-authorized |
| Local audio | built-in mic/speakers, **half-duplex** (mic gated off while agent speaks); wake word via **openWakeWord `hey_jarvis`** (onnx) |
| Subagent model | `claude-opus-5` default; `dispatch_task.model` accepts `opus`/`sonnet`/`fable`/`haiku` or a full model id |
| Inbound SMS | Out of scope (SMS is outbound summaries only) |
| Language / tooling | Python 3.12, `uv`, FastAPI + uvicorn, typer, pytest (+ pytest-asyncio), ruff |
| Repo | this folder; GitHub private repo `garmin-voice-agent` |

Prerequisites William supplies (in `.env`): `OPENAI_API_KEY`, subagent auth (the Claude
CLI subscription login by default; `ANTHROPIC_API_KEY` is the pay-per-token override —
amended 2026-08-24, was: the Agent SDK cannot use the subscription login), Twilio account
SID / auth token / number, a Cloudflare-routed hostname for the tunnel, Google Cloud
OAuth client (Gmail + Calendar scopes).

## 3. Architecture

```
Phone/Watch ─PSTN─▶ Twilio ─WSS media stream─▶ Cloudflare Tunnel ─▶ FastAPI (Linux)
                                                          │
Mac mic ── openWakeWord "hey jarvis" ──▶ LocalAudioDevice / LocalTransport   (macOS)
                                                          ▼
                                    VoiceSession  (transport-agnostic core)
                               audio pump ▲▼   tool calls   ▲ announce()
                                          │                 │
                            OpenAIRealtimeClient        Notifier ◀── EventBus
                                          │                 ▲
                                    ToolRegistry ──▶ TaskManager ──▶ Claude Agent SDK
                                                          │  (one AgentSession per task)
                                                          ▼
                                             SQLite tasks  +  Twilio SMS / call-back
```

### 3.1 Package layout (`src/jarvis/`)

| Module | Responsibility |
|---|---|
| `config.py` | `Settings` (pydantic-settings): keys, Twilio numbers, allowlist, PIN, public host, projects, voice/model names, timeouts, concurrency, guardrails |
| `projects.py` | `discover_projects` (configured projects plus `projects_root` subdirectories, shared by `TaskManager` and the voice prompt) and `discover_briefs` (each project's own `.jarvis-brief.md`) |
| `transcripts.py` | `read_tail`: the end of an earlier call, read back out of `data_dir/calls/<session_id>.log` for a call-back's opening context |
| `briefing.py` | `Briefer`/`Briefing`: what a call opens knowing — the digest of finished-but-unreported tasks, and `data_dir/memory.md` |
| `recall.py` | `Recaller`: keyword search across past call transcripts and past task summaries, behind the voice model's `recall` tool |
| `memory.py` | `MemoryWriter`: on `SessionEnded`, dispatches the internal subagent that folds the call into `data_dir/memory.md` |
| `skills.py` | `discover_skills`: the Claude skills installed on the machine (name + description from each `SKILL.md`), listed in the voice prompt |
| `web_search.py` | `WebSearcher` protocol + `OpenAIWebSearch` (Responses API, hosted `web_search` tool), behind the voice model's own `web_search` tool |
| `slack.py` | `SlackSender` protocol + `SlackWebApi` (`chat.postMessage`), behind the voice model's `send_to_slack`; credentials resolve from the `auto-research` skill's MCP server config |
| `restart.py` | `RestartCoordinator`: restart this service through systemd/launchd, and call back once it is up; `RestartStore` (the record that survives the restart), `resolve_target`, `health_probe`, `watch_command`/`spawn_watchdog` (arming the watchdog below) |
| `restart_watch.py` | `watch`: the out-of-process watchdog armed by a restart, which alerts by text and a plain `<Say>` call when the service never comes back |
| `logscan.py` | `marks`/`errors_since`: the service's own log files, scoped by byte offset to what happened since a restart was asked for |
| `events.py` | in-process async pub/sub `EventBus` + event dataclasses |
| `audio/util.py` | soxr resampling, chunk helpers, `AudioGate` (half-duplex state machine), `PlaybackBuffer` (µ-law codec removed 2026-08-19: phone audio is passed through as `audio/pcmu`, nothing transcodes) |
| `transports/base.py` | `Transport` protocol + `AudioIn`/`Dtmf`/`Hangup` events |
| `transports/twilio_ws.py` | Twilio media-stream WS transport (µ-law passthrough) |
| `transports/local_audio.py` | `LocalAudioDevice` (sounddevice mic/speaker, gate, chime) + `LocalTransport` (one session over the device) |
| `wakeword.py` | `WakeWordDetector` protocol; `OpenWakeWordDetector`; `WakeWordListener` loop |
| `local_runner.py` | idle (wake-word) ⇄ session state machine for the local channel |
| `realtime/base.py` | `RealtimeProvider` protocol, `SessionConfig`, typed provider events |
| `realtime/openai.py` | OpenAI Realtime WS client (GA schema) |
| `session.py` | `VoiceSession` (wires transport⇄provider, barge-in, tool dispatch, PIN gate, announcements, lifecycle, transcript log) + `SessionRegistry` |
| `tools/registry.py` | `ToolRegistry`, `ToolContext`; `tools/builtin.py` registers the tool set |
| `tasks/models.py` | `Task` (schema v4: `reported_at`, `internal`, `needs_restart`), `TaskKind`, `TaskStatus` |
| `tasks/store.py` | SQLite store (`TaskStore`) |
| `tasks/agent_runner.py` | `AgentRunner` protocol; `ClaudeAgentRunner` (Agent SDK); `FakeAgentRunner` (tests) |
| `tasks/manager.py` | `TaskManager`: queue/semaphore, lifecycle, follow-up, cancel, logs, events |
| `notify/notifier.py` | routes task results: live sessions → SMS → call-back, or hands the call-back to a restart when the work changed Jarvis's own code |
| `notify/twilio_out.py` | SMS + outbound call (TwiML with `<Parameter>`) |
| `server.py` | FastAPI app: `/twilio/voice`, `/twilio/media`, `/twilio/status`, `/health`, `/reports/{id}` |
| `app.py` | `AppState` composition root (settings → store, bus, manager, registry, notifier, session registry) |
| `prompts/voice_system.md` | receptionist persona + tool-use guidance |
| `prompts/subagent_suffix.md` | appended to Agent SDK system prompt: autonomous, ends with `SPOKEN_SUMMARY:` block |
| `prompts/memory_update.md` | the internal memory subagent's prompt: merge this call's transcript into `memory.md`, keep the structure, stay under budget |
| `cli.py` | `jarvis serve`, `loopback`, `download-models`, `tasks list|show`, `memory`, `setup-google`, `doctor`, `restart`, `restart-watch` (hidden; armed by a restart, not run by hand) |

### 3.2 Binding interfaces

These signatures are shared across tasks; implementers must match them exactly (adding
optional keyword args is fine, renaming is not).

```python
# events.py
class EventBus:
    def subscribe(self, event_type: type, handler) -> Callable[[], None]   # handler sync or async; returns unsubscribe
    async def publish(self, event) -> None                                  # awaits async handlers; exceptions logged, never propagated

@dataclass class TaskStarted:   task_id: int
@dataclass class TaskProgress:  task_id: int; text: str
@dataclass class TaskCompleted: task_id: int; summary: str
@dataclass class TaskFailed:    task_id: int; error: str
@dataclass class SessionStarted: session_id: str; channel: str; caller: str | None
@dataclass class SessionEnded:   session_id: str; channel: str; caller: str | None; reason: str
```

```python
# transports/base.py
AudioFormat = Literal["audio/pcmu", "audio/pcm"]      # pcmu = G.711 µ-law 8 kHz; pcm = 16-bit LE mono 24 kHz
@dataclass class AudioIn: data: bytes; timestamp_ms: int | None = None   # timestamp = transport clock (Twilio media.timestamp), else None
@dataclass class Dtmf:    digit: str
@dataclass class Hangup:  reason: str
TransportEvent = AudioIn | Dtmf | Hangup

class Transport(Protocol):
    channel: Literal["phone", "local"]
    caller: str | None                    # E.164 for phone; None for local
    audio_format: AudioFormat             # same for in and out
    def events(self) -> AsyncIterator[TransportEvent]: ...
    async def send_audio(self, data: bytes) -> None: ...   # enqueue for playback
    async def clear(self) -> None: ...                     # drop queued playback (barge-in)
    async def hangup(self) -> None: ...                    # end the call/session; events() finishes
```

```python
# realtime/base.py
@dataclass class SessionConfig:
    instructions: str
    tools: list[dict]                       # OpenAI function-tool schemas: {"type":"function","name","description","parameters"}
    voice: str
    audio_format: AudioFormat               # used for both input and output
    vad_mode: Literal["server","semantic"] = "semantic"   # amended 2026-08-24 (was: server only)
    vad_eagerness: Literal["low","medium","high","auto"] = "low"
    vad_threshold: float = 0.5
    vad_silence_ms: int = 500                # server mode only; semantic_vad rejects it
    vad_prefix_ms: int = 300
    interrupt_response: bool = True         # False for local (half-duplex)
    transcription_model: str | None = "gpt-4o-mini-transcribe"

@dataclass class AudioDelta:     item_id: str; audio: bytes                    # decoded bytes in session audio_format
@dataclass class SpeechStarted:  item_id: str | None; audio_start_ms: int
@dataclass class SpeechStopped:  pass
@dataclass class ResponseStarted: response_id: str
@dataclass class ResponseDone:   response_id: str; status: str
@dataclass class FunctionCall:   call_id: str; name: str; arguments: dict
@dataclass class Transcript:     role: Literal["user","assistant"]; text: str; item_id: str | None
@dataclass class ProviderError:  code: str | None; message: str; fatal: bool
@dataclass class Disconnected:   reason: str
ProviderEvent = Union[...all of the above...]

class RealtimeProvider(Protocol):
    async def connect(self, config: SessionConfig) -> None
    async def close(self) -> None
    def events(self) -> AsyncIterator[ProviderEvent]          # ends after Disconnected
    async def send_audio(self, data: bytes) -> None            # bytes in config.audio_format
    async def submit_tool_result(self, call_id: str, output: dict | str) -> None   # creates function_call_output item + requests a response
    async def inject_message(self, text: str, *, respond: bool = True, response_instructions: str | None = None) -> None
    async def truncate(self, item_id: str, audio_end_ms: int) -> None
    async def cancel_response(self) -> None
    async def reconnect(self) -> bool                          # one attempt: re-open WS + re-send session config
```

Provider rule: **only one active response at a time.** `submit_tool_result` and
`inject_message(respond=True)` go through an internal response queue: if a response is
active (between `response.created` and `response.done`), the `response.create` is queued
and sent when the active response finishes. Items (`conversation.item.create`) are sent
immediately.

```python
# tools/registry.py
@dataclass class ToolContext:
    session: "VoiceSession"           # duck-typed: needs .authorized, .channel, .caller, .session_id, .request_end(), .authorize()
    channel: str
    caller: str | None
    @property authorized -> bool

ToolHandler = Callable[[ToolContext, dict], Awaitable[dict]]
class ToolRegistry:
    def register(self, name: str, description: str, parameters: dict, handler: ToolHandler) -> None
    def schemas(self) -> list[dict]
    async def call(self, name: str, arguments: dict, ctx: ToolContext) -> dict   # unknown tool / exception → {"error": "..."}; never raises
```

```python
# tasks/models.py
class TaskKind(StrEnum): AGENT="agent"     # one kind; `_missing_` maps pre-2026-08-24 rows onto it
class TaskStatus(StrEnum): QUEUED="queued"; RUNNING="running"; DONE="done"; FAILED="failed"; CANCELLED="cancelled"
# No DESTRUCTIVE_KINDS: every task has the machine and the mailbox, so the phone PIN gates all of them.

@dataclass class Task:
    id: int | None; kind: TaskKind; description: str; status: TaskStatus = QUEUED
    project: str | None = None; cwd: str | None = None; model: str = "claude-opus-5"
    claude_session_id: str | None = None; summary: str | None = None; report_path: str | None = None
    error: str | None = None
    origin_channel: str = "local"; origin_caller: str | None = None
    callback_requested: bool = False; callback_number: str | None = None
    announced: bool = False; sms_sent: bool = False
    created_at: datetime; started_at: datetime | None; finished_at: datetime | None
```

```python
# tasks/store.py
class TaskStore:
    def __init__(self, path: Path | str)                # ":memory:" allowed for tests
    async def create(self, task: Task) -> Task            # assigns id
    async def get(self, task_id: int) -> Task | None
    async def update(self, task_id: int, **fields) -> Task
    async def list(self, *, status=None, limit=20, include_internal=False) -> list[Task]  # newest first
    async def count_created_since(self, since: datetime) -> int   # excludes internal (the daily cap)
    async def list_unreported(self, *, limit=MAX_UNREPORTED) -> list[Task]   # done/failed, oldest first
    async def count_unreported(self) -> int
    async def mark_reported(self, task_ids: Iterable[int], *, when: datetime) -> list[int]  # ids stamped
    async def search(self, terms: Sequence[str], *, limit=5) -> list[Task]   # every term, newest first
    async def list_for_session(self, session_id: str, *, limit=20) -> list[Task]
    async def close(self) -> None
```

```python
# tasks/agent_runner.py
@dataclass class RunResult:
    ok: bool; final_text: str; spoken_summary: str; session_id: str | None; cost_usd: float | None; error: str | None

class AgentSession(Protocol):                                     # one live subagent conversation
    async def run(self, prompt: str, *, on_progress: Callable[[str], Any]) -> RunResult   # one turn to completion; ok=False on error
    async def send(self, text: str) -> None                       # follow-up while running (queued into the live conversation)
    async def interrupt(self) -> None
    async def close(self) -> None

class AgentRunner(Protocol):
    async def open(self, task: Task, *, resume: str | None = None) -> AgentSession

class ClaudeAgentRunner(AgentRunner): ...                          # real Agent SDK
class FakeAgentRunner(AgentRunner): ...                            # scripted, for tests

def extract_spoken_summary(text: str) -> str                      # SPOKEN_SUMMARY: block → else last paragraph (≤ 400 chars)
```

```python
# tasks/manager.py
class TaskManager:
    def __init__(self, store: TaskStore, runner: AgentRunner, bus: EventBus, settings: Settings)
    async def start(self) -> None; async def shutdown(self) -> None
    async def dispatch(self, description, *, project=None, model=None, origin_channel, origin_caller) -> Task   # raises TaskLimitError / UnknownProjectError
    async def wait_for(self, task_id: int, timeout: float) -> Task     # returns as soon as terminal or timeout
    async def followup(self, task_id: int, text: str) -> Task
    async def cancel(self, task_id: int) -> Task
    async def get(self, task_id: int) -> Task | None
    async def list(self, *, status=None, limit=20) -> list[Task]
    def resolve_project(self, name: str) -> tuple[str, Path]           # raises UnknownProjectError
    def list_projects(self) -> list[tuple[str, Path]]
```

```python
# session.py
class VoiceSession:
    def __init__(self, transport, provider, settings, tools: ToolRegistry, bus: EventBus, *,
                 authorized: bool, opening_context: str | None = None, session_id: str | None = None)
    session_id: str; channel: str; caller: str | None; authorized: bool
    async def run(self) -> None                       # returns when session ends
    async def announce(self, text: str) -> bool       # inject + speak; False if session not live
    def request_end(self, reason: str = "user") -> None
    def authorize(self) -> None

class SessionRegistry:
    def add(self, s: VoiceSession); def remove(self, s: VoiceSession); def live(self) -> list[VoiceSession]
```

### 3.3 Key behaviors

- **Session start (phone)**: Twilio → `POST /twilio/voice` validates signature + allowlist,
  mints a one-time stream token, returns TwiML
  `<Connect><Stream url="wss://HOST/twilio/media"><Parameter name="token"…/><Parameter name="caller"…/></Stream></Connect>`.
  WS `start` carries `customParameters`; token must match a pending token (single-use, 60 s
  TTL) or the socket is closed. Then `VoiceSession(TwilioTransport)` runs; the agent greets
  first. Outbound call-backs carry `task_id` so the session opens with the summary.
- **Session start (local)**: wake word → chime → `VoiceSession(LocalTransport)` (pre-authorized)
  → short greeting ("Yes?").
- **Barge-in (phone)**: `SpeechStarted` → `transport.clear()` + `provider.truncate(item_id, played_ms)`
  where `played_ms` = transport clock delta since the current item's first audio delta
  (Twilio `media.timestamp`), else wall clock; capped at total ms sent for that item. Local
  transport is half-duplex: mic gated off while playing (+200 ms hangover) → no barge-in,
  `interrupt_response=False`.
- **dispatch_task(description, project?, model?, wait_seconds≤25)**: creates the task and
  starts it; if it finishes within `wait_seconds` the summary is returned inline; else returns
  `{task_id, status:"running"}` and the model tells the user it will announce completion.
- **One routing decision (amended 2026-08-24).** The voice model either answers the turn
  itself — small talk, task status, and small factual questions through `web_search` — or
  dispatches it. There is no kind to choose and no second axis. Each project may describe
  itself to the voice model in a `.jarvis-brief.md` at its root, which the prompt carries
  verbatim; a repository's `CLAUDE.md` is deliberately *not* used for this.
- **Handing over beats interviewing (amended 2026-08-24).** The voice model dispatches code work
  the moment it recognises it — no repeat-back-and-confirm, no scoping questions — because the
  subagent is better placed to work out what the work needs. `coding` with no project no longer
  raises: the task starts in `projects_root` and the subagent finds the repo itself. The voice
  prompt lists every project `discover_projects` can resolve (not just the configured ones) and
  every installed skill, so neither has to be named out loud.
- **Written delivery goes over Slack (added 2026-08-24).** A phone call cannot carry a file,
  a link or a long list. The voice model has `send_to_slack` for text; subagents use the
  `slack-research` MCP server (the `auto-research` skill's, inherited user-scope by every CLI
  the runner spawns — verified, along with `google` and the claude.ai connectors) and its
  `slack_upload_file` for anything with a file in it. Both ends use one Slack app: the token
  and DM channel come from `SLACK_BOT_TOKEN` / `SLACK_CHANNEL_ID` if set, else from that MCP
  server's own config in `~/.claude.json`.
- **Questions come back from the subagent.** The subagent stays autonomous by default, but where
  a decision is genuinely the user's it does the independent part first and ends its
  `SPOKEN_SUMMARY:` with one spoken question. The voice model asks it and returns the answer via
  `send_followup`, which resumes the same Claude session.
- **A call-back is a new call, handed the old one.** The provider keeps nothing across
  sockets, so the opening context is assembled from what survives: the original request, the
  result, the `callback_note` the earlier session left, and the tail of that session's
  transcript, found through `Task.origin_session_id` (added 2026-08-24, with `callback_note`,
  as schema v2). The session still starts unauthorized — the PIN is asked for again.
- **Offering the call-back (added 2026-08-24)**: when a task is still running and the caller
  has nothing more to add, the voice model offers `request_callback` itself rather than waiting
  to be asked — holding the line for a long job is the worst use of a call from a watch.
- **Task completion**: `TaskCompleted` → Notifier: (1) `announce()` on every live session
  except the one currently inline-waiting on that task inside `dispatch_task` (it gets the
  result as the tool output instead) (marks `announced`), (2) SMS summary (with report link) unless a live *phone* session
  announced it, (3) if `callback_requested` and no live phone session announced → outbound
  call with `task_id`.
- **Restarting is two halves and a file (added 2026-08-24).** The process that runs
  `systemctl restart` is the process that gets killed, so `restart_service` / `jarvis restart`
  writes `data_dir/restart.json` (what was asked for, by whom, on which number, which version
  was running) *before* handing over, and `RestartCoordinator.resume()` — one task per
  `jarvis serve` — is what finds it on the other side and confirms it. The confirmation is a
  call, carrying a one-line status summary (how long it was down, the version then and now,
  which channels are listening, how many tasks the restart interrupted) as the new session's
  opening context. Neither half ever interrupts a call: a restart asked for *during* one waits
  for the line to clear (and abandons itself rather than cut a call off), and the confirmation
  is announced into a live session, or texted, rather than dialled into one. **Corrected
  2026-08-26:** "clear" for the restart means no live session *and* no task in `running` — a
  restart kills every subagent it finds and nothing resumes them, and waiting only for the
  line made the moment a call ends, which is when the memory update is dispatched, the most
  dangerous moment to restart in. `--force` still overrides, a `queued` task does not count
  (it has not started), and a task store that will not answer is read as "nothing running"
  rather than allowed to wedge the restart. The *confirmation*'s wait is unchanged and still
  only about the line. Attempts are
  counted on the record before the dial, so a crash loop rings once, not once per crash; a call
  that cannot be placed falls back to SMS; and a confirmation that fails outright leaves the
  record behind as `failed`, for `jarvis restart --status`. A process nothing supervises refuses
  to restart at all — stopping would take it off the air for good.
- **A restart says whether the *update* worked, not whether the process came back (added
  2026-08-26).** Restarting is mostly asked for to load a change Jarvis has just made to its
  own code, and "back up" answers the wrong question: an import that throws, a tool that fails
  to register or a credential that broke all come back up and answer `/health`. Three things
  close that, and each covers a failure the others cannot see.
  1. **The logs, scoped.** The process asking for a restart records how long each service log
     file is (`logscan.marks`, onto `restart.json`); the process that comes back reads from
     there. Tracebacks collapse to the line that ends them, and the errors go into the status
     summary *before* the housekeeping clauses, because they are the answer to the only
     question a restart is asked. No marks on the record (an older one) means "nothing found",
     never the whole file.
  2. **The task.** `restart_service` takes the `task_id` whose change it is loading. That names
     the change on the confirmation, and licenses the other check worth making: a restart asked
     for to load a change, running the same checkout as before, has loaded nothing.
  3. **A watchdog outside the cgroup.** A change that will not import means no new process, so
     no `resume()`, so no call and no text — silence that reads exactly like success.
     `_execute()` therefore arms `jarvis restart-watch` through `systemd-run --user`
     (`launchd` needs only a new session) *immediately before* handing over — not at request
     time, or a deferred restart would outlive its own watch. It acts only on the case nothing
     else can see, a record still `pending` at the deadline: it marks the record `failed` so a
     service that limps up an hour later does not ring about it, texts the detail with whatever
     the logs said, and rings with plain `<Say>` TwiML — every other call Jarvis places is
     answered by the media stream of the server that is not running. Nothing to arm it with
     (no `systemd-run`) is written on the record rather than papered over.

  The two halves read the logs from **different marks**, because they ask different
  questions (corrected 2026-08-26). The watchdog reads from the *request*, because there may
  be no new process to have marked anything. `resume()` reads from `mark_startup_logs()`,
  stamped at the top of `jarvis serve`, because its question is "did I come up clean" and
  the request's mark also catches the dying process's last gasps — a Python shutting down
  with a subagent subprocess still open reliably prints `RuntimeError: Event loop is closed`
  out of `base_subprocess.__del__`, which put "but 1 error in the log since" on the
  confirmation for a restart that had gone perfectly, and would have done so on every
  self-edit restart.
  A negative exit code from the restart command is **not** a failure: `systemctl` sits in the
  cgroup the restart tears down, so it is killed handing over and returns `-15`. Read as a
  failure (as it was until 2026-08-26) it wrote `systemctl exited -15` onto a service that had
  come back fine, and then said nothing at all, the process that would have spoken being the
  one dying.
- **A task that changed Jarvis's own code hands its call-back to the restart (added
  2026-08-26).** The subagent declares it, with a `RESTART_REQUIRED: <why>` line above its
  `SPOKEN_SUMMARY:` — it is the only thing that knows, since `git describe --dirty` flips on
  any edit anyone has open and the voice model can only guess from a spoken summary. It is a
  request and not a fact about the checkout, honoured only on a task that *succeeded* and
  never on an `internal` one: nothing Jarvis dispatches to itself may take Jarvis off the
  air.
- **`queued` is not a waiting room, so a queued row is resumed (added 2026-08-26).** A task
  is *born* `queued` and flips to `running` about a second later, when the coroutine
  `dispatch` created takes the semaphore; under the concurrency cap nothing normally waits
  there at all. A row still `queued` in a fresh process is therefore one that never executed
  a single instruction, and `TaskManager.resume_queued()` — one call per `jarvis serve`,
  oldest first — is running it for the first time rather than twice. `running` rows are the
  opposite and stay lost: that subagent had opened, and what it got through is unknowable.
  The drain runs *after* the restart confirmation, so the confirmation's count is of what
  the old process left rather than of what this one has already started. `TaskManager` records it as `Task.needs_restart` and acts on nothing; the Notifier is
  what decides, because it is the thing that knows whether he is mid-call. It announces and
  texts the result as usual (both cost nothing and both survive a restart that does not come
  back), then arms the restart with the `task_id` and returns *instead of* placing its own
  call-back — the restart's confirmation is a better one, because it can also say whether the
  change is running, and two calls a minute apart about one piece of work is the alternative.
  `needs_restart` and `callback_requested` are both cleared so a follow-up cannot restart
  twice or dial twice. A restart that is refused (`unsupported`, `failed`) is not a call-back,
  so the ordinary one still goes out: refusing to restart must not also swallow the result.
- **A finished task is not delivered until Jarvis has said it (added 2026-08-25).**
  `announced` and `sms_sent` record that a *delivery was attempted*; neither survives a call
  he missed or a text he never read. `Task.reported_at` records that the voice model actually
  told him, and it is stamped by exactly one thing: the `mark_reported` tool, which the model
  calls after speaking the result. Until then the task is in `TaskStore.list_unreported()`
  (done/failed only, oldest first, capped at `MAX_UNREPORTED`) and `Briefer` puts it at the
  top of the next call — in the system prompt under "What he has not heard yet", and as a
  one-line nudge appended to the opening message, because a realtime model leads with what it
  was just handed. Ruling: no other code path stamps `reported_at`. A model that forgets the
  tool costs him hearing something twice; a delivery flag that stamps it costs him never
  hearing it at all, and only one of those is recoverable. Rows that were already terminal
  when the v3 migration ran are back-filled as reported, so the first call after an upgrade
  is not a recital of the whole history.
- **Continuity across calls is a memory file a subagent writes (added 2026-08-25).** The
  provider keeps no history across sockets, so `MemoryWriter` subscribes to `SessionEnded` and
  dispatches a subagent whose only job is to fold the call that just ended into
  `data_dir/memory.md` (`prompts/memory_update.md`); `Briefer` reads it back into the next
  call's prompt, with its headings nested one level so its sections cannot read as
  instructions. A call with fewer than `MIN_SPOKEN_LINES` spoken lines is a misfire and gets
  no subagent. It is a real subagent rather than a summarising API call because the memory is
  worth more when whoever writes it can go and look — at the task's report, at the repo, at
  whether the thing he was waiting on has landed.
- **`Task.internal` is not a task kind (added 2026-08-25).** Work Jarvis asked for itself —
  today only the memory update — is dispatched `internal=True`. That keeps it out of
  `list_tasks`, out of `list_unreported`, out of `search`, out of `count_created_since` (so it
  cannot eat `DAILY_TASK_CAP`), and out of the Notifier entirely: he never asked for it, so
  announcing, texting or ringing him about it would be Jarvis interrupting him to talk about
  Jarvis. It restricts *nothing* about what that subagent may do, which is what keeps it
  distinct from the task kinds removed on 2026-08-24. `jarvis tasks list --internal` shows it.
- **`recall` answers questions about the past without a subagent (added 2026-08-25).**
  `Recaller` searches the call transcripts in `data_dir/calls/` and the store's descriptions
  and summaries, and hands back a handful of dated snippets. Matching is whole-substring
  conjunction over stop-word-filtered terms: the query arrives as speech, already mangled once
  by transcription, and fuzzy matching on top of that produces confident nonsense that is then
  read out loud. Empty results are a `message` telling the model to say it has nothing, never
  a guess. Transcript lines carry a full ISO timestamp as of this change so a hit can be
  dated; older lines stamped with a wall clock alone fall back to the file's mtime.
- **PIN gate**: an empty/blank `JARVIS_PIN` counts as *not configured* (dispatching refused on phone). `session.authorized` starts False on phone; `submit_pin` (spoken) or DTMF
  digits (collected in the session, never shown to the model) flip it; `dispatch_task`
  returns `{"status":"pin_required"}` until authorized — every task, since 2026-08-24, because
  every task can reach the files and the mailbox. Constant-time
  compare; 3 failures → say goodbye and hang up.
- **Follow-ups**: `send_followup(task_id, text)` → finished task: new run with
  `resume=claude_session_id`; running task: the text is queued and, when the current run
  finishes, the task is immediately re-run with `resume` and the queued follow-ups as the
  prompt (no completion announcement for the intermediate result). (Ruling 2026-08-19: the
  SDK's mid-turn `query()` semantics are unverified, so `AgentSession.send()` is not used
  for live follow-ups.) PIN gate applies to `send_followup`/`cancel_task` on destructive
  kinds exactly as to `dispatch_task`.
- **Concurrency**: `MAX_CONCURRENT_TASKS` (default 3); overflow tasks stay `queued`.
- **Local session end**: `end_session` tool, or `LOCAL_SILENCE_TIMEOUT` (30 s without user speech
  after the last response) → goodbye → back to wake-word listening.
- **Reconnects**: provider WS drop mid-call → one `reconnect()`; on success inject
  "[system] connection was reset; briefly apologize and continue"; on failure end session.
  Twilio WS drop → session teardown; tasks keep running.
- **Guardrails**: `MAX_CALL_SECONDS` (default 1800), `SUBAGENT_MAX_TURNS`, `SUBAGENT_MAX_BUDGET_USD`,
  `DAILY_TASK_CAP`.

### 3.4 Configuration (`.env` names → `Settings` fields)

| Env | Field | Default |
|---|---|---|
| `OPENAI_API_KEY` | `openai_api_key` | required |
| `OPENAI_REALTIME_MODEL` | `openai_realtime_model` | `gpt-realtime-2.1` |
| `OPENAI_VOICE` | `openai_voice` | `cedar` (male; the API takes `alloy`, `ash`, `ballad`, `coral`, `echo`, `sage`, `shimmer`, `verse`, `marin`, `cedar`) |
| `OPENAI_TRANSCRIPTION_MODEL` | `openai_transcription_model` | `gpt-4o-mini-transcribe` |
| `OPENAI_WEB_SEARCH_MODEL` | `openai_web_search_model` (answers the voice model's `web_search`) | `gpt-5.4-mini` |
| `ANTHROPIC_API_KEY` | `anthropic_api_key` | `None` |
| `SUBAGENT_MODEL` | `subagent_model` | `claude-opus-5` |
| `SUBAGENT_MAX_TURNS` | `subagent_max_turns` | `200` |
| `SUBAGENT_MAX_BUDGET_USD` | `subagent_max_budget_usd` | `10.0` |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_NUMBER` | `twilio_account_sid` / `twilio_auth_token` / `twilio_number` | `None` |
| `ALLOWED_CALLERS` | `allowed_callers: list[str]` (comma-separated E.164) | `[]` |
| `OWNER_NUMBER` | `owner_number` | first of `allowed_callers` |
| `JARVIS_PIN` | `pin` | `None` (PIN-gated kinds refused on phone if unset) |
| `PUBLIC_HOST` | `public_host` (the tunnel's hostname, e.g. `jarvis.example.com`) | `None` |
| `HOST` / `PORT` | `host` / `port` | `127.0.0.1` / `8080` |
| `SERVICE_MANAGER` | `service_manager` (`auto`/`systemd`/`launchd`/`none`; what `jarvis restart` asks) | `auto` → systemd on Linux, launchd on macOS, none if neither is on PATH |
| `SERVICE_UNIT` | `service_unit` (the unit/label to restart) | `None` → `jarvis.service` / `com.william.jarvis` |
| `PROJECTS` | `projects: dict[str,str]` (JSON) | `{}` |
| `PROJECTS_ROOT` | `projects_root` | `~/Local/coding_projects` |
| `SKILLS_DIR` | `skills_dir` (Claude skills listed in the voice prompt) | `~/.claude/skills` |
| `DATA_DIR` | `data_dir` | `~/.jarvis` |
| `MAX_CONCURRENT_TASKS` | `max_concurrent_tasks` | `3` |
| `DISPATCH_WAIT_MAX_SECONDS` | `dispatch_wait_max_seconds` | `25` |
| `LOCAL_SILENCE_TIMEOUT` | `local_silence_timeout` | `30` |
| `VAD_MODE` | `vad_mode` (`semantic` waits on a finished sentence, `server` on a silence timer) | `semantic` |
| `VAD_EAGERNESS` | `vad_eagerness` (semantic mode: how soon it jumps in) | `medium` (was `low` until 2026-08-26 — about two seconds of silence per turn) |
| `NOISE_REDUCTION` | `noise_reduction` (`auto`/`near_field`/`far_field`/`off`) | `auto` → `near_field` on the phone, `far_field` on the local mic |
| `VAD_SILENCE_MS` / `VAD_THRESHOLD` / `VAD_PREFIX_MS` | `vad_silence_ms` / `vad_threshold` / `vad_prefix_ms` (server mode) | `1200` / `0.5` / `300` |
| `MAX_CALL_SECONDS` | `max_call_seconds` | `1800` |
| `DAILY_TASK_CAP` | `daily_task_cap` | `50` |
| `WAKEWORD_MODEL` / `WAKEWORD_THRESHOLD` | `wakeword_model` / `wakeword_threshold` | `hey_jarvis` / `0.5` |
| `REPORT_SECRET` | `report_secret` | `None` → random secret persisted at `data_dir/report_secret` |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` / `USER_GOOGLE_EMAIL` | same names lowercased | `None` |
| `GOOGLE_CLIENT_SECRETS_FILE` | `google_client_secrets_file` (used when the id/secret pair is unset) | `.secrets/client_secret.json` |
| `GOOGLE_WORKSPACE_MCP` | `google_workspace_mcp` (attach the `workspace-mcp` server to subagents) | `false` |
| `SLACK_BOT_TOKEN` / `SLACK_CHANNEL_ID` | `slack_bot_token` / `slack_channel_id` | `None` → the `slack-research` MCP server's config |
| `LOG_LEVEL` | `log_level` | `INFO` |

Data layout under `data_dir`: `tasks.db`, `tasks/<id>.log` (agent transcript),
`tasks/<id>.md` (final report), `calls/<session_id>.log` (voice transcript),
`report_secret`, `restart.json` (0600; the pending restart's call-back, its log marks and
its watchdog), `memory.md` (what Jarvis remembers between calls), `logs/jarvis.log` (our own
rotated handler) alongside the `jarvis.out.log`/`jarvis.err.log` the service unit appends to
and `logs/restart-watch.log` (the watchdog's own output), `google/` (MCP credentials).

## 4. Verified API notes (Aug 2026)

**OpenAI Realtime (GA)** — `wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1`,
header `Authorization: Bearer …`, **no** `OpenAI-Beta` header.
- `session.update`: `{"type":"session.update","session":{"type":"realtime","instructions":…,
  "tools":[{"type":"function","name":…,"description":…,"parameters":{…}}],"tool_choice":"auto",
  "audio":{"input":{"format":{"type":"audio/pcmu"},"turn_detection":{"type":"server_vad",
  "threshold":0.5,"prefix_padding_ms":300,"silence_duration_ms":500,"create_response":true,
  "interrupt_response":true}  — or `{"type":"semantic_vad","eagerness":"low",…}`, which takes no
  `silence_duration_ms` (the API rejects it) and ends the turn on the *sense* of the sentence,"transcription":{"model":"gpt-4o-mini-transcribe"},
  "noise_reduction":{"type":"near_field"}},
  "output":{"format":{"type":"audio/pcmu"},"voice":"marin"}}}}`.
  Formats: `audio/pcmu`, `audio/pcma` (8 kHz G.711 — Twilio path, no transcoding),
  `audio/pcm` (24 kHz 16-bit LE mono — local path). **Corrected 2026-08-24:** `audio/pcm`
  must carry its rate — `{"type":"audio/pcm","rate":24000}` — or the session is refused with
  `missing_required_parameter: session.audio.input.format.rate`; the G.711 formats must
  *not* carry one (`Unknown parameter`). Verified against the live GA API.
- `audio.input.noise_reduction` takes `{"type":"near_field"}` or `{"type":"far_field"}` and
  nothing else — a bogus value is refused with `Supported values are: 'near_field' and
  'far_field'`, so it is a validated field and not one the API quietly ignores. Absent is how
  it is turned off; the session reports it back as `null` when unset (verified 2026-08-26
  against the live GA API).
- Session tools accept **only** `{"type":"function"}` and `{"type":"mcp"}` — there is no hosted
  `web_search` in a Realtime session (verified 2026-08-24; the hosted tool lives in the Responses API,
  which is what `web_search.py` calls).
- Client events: `input_audio_buffer.append{audio:b64}`, `conversation.item.create{item}`,
  `conversation.item.truncate{item_id,content_index:0,audio_end_ms}`, `response.create{response?}`,
  `response.cancel`.
- Server events: `session.created`, `session.updated`, `input_audio_buffer.speech_started{item_id,audio_start_ms}`,
  `input_audio_buffer.speech_stopped`, `response.created{response.id}`,
  `response.output_audio.delta{item_id,delta:b64}`, `response.output_audio_transcript.done{item_id,transcript}`,
  `conversation.item.input_audio_transcription.completed{item_id,transcript}`,
  `response.function_call_arguments.done{call_id,name,arguments:json-string}`,
  `response.output_item.done{item:{type:"function_call",…}}`, `response.done{response.id,response.status}`,
  `error{error:{type,code,message}}`, `rate_limits.updated`.
- Function calling: `conversation.item.create{item:{type:"function_call_output",call_id,output:"<json string>"}}` → `response.create`.
- Unprompted speech: `conversation.item.create{item:{type:"message",role:"system",content:[{type:"input_text",text}]}}` + `response.create{response:{instructions}}`.

**Twilio media streams** — TwiML `<Connect><Stream url="wss://HOST/twilio/media"><Parameter name="…" value="…"/></Stream></Connect>`
(`url` cannot carry a query string). Inbound WS messages: `connected`, `start{streamSid,callSid,customParameters,mediaFormat}`,
`media{media:{payload:b64 µ-law, timestamp:"ms"}}`, `dtmf{dtmf:{digit}}`, `mark{mark:{name}}`, `stop`.
Outbound: `{"event":"media","streamSid",…,"media":{"payload":b64}}`, `{"event":"mark","streamSid",…,"mark":{"name"}}`,
`{"event":"clear","streamSid"}`. Signature: `twilio.request_validator.RequestValidator(auth_token).validate(url, params, signature)`
with URL rebuilt from `x-forwarded-proto` / `x-forwarded-host` when present. Python:
`Client(sid, token).messages.create(from_=, to=, body=)`, `client.calls.create(to=, from_=, twiml=, status_callback=)`.

**openWakeWord 0.6.0** — `openwakeword.utils.download_models(model_names=["hey_jarvis"])` once;
`Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")`; `predict(int16 ndarray of 1280 samples @16 kHz)`
returns `{model_name: score}` (key is `hey_jarvis` in 0.6.0 — take the max over values, don't hardcode).

**Claude Agent SDK (`claude-agent-sdk` 0.2.x)** — needs `claude` CLI on PATH + `ANTHROPIC_API_KEY`.
`ClaudeSDKClient(ClaudeAgentOptions(permission_mode="bypassPermissions", cwd=…, setting_sources=["user","project"],
system_prompt={"type":"preset","preset":"claude_code","append": SUFFIX}, model=…, mcp_servers={…}, allowed_tools=[…],
max_turns=…, max_budget_usd=…, resume=<session_id>, env={…}))`; `await client.connect()`, `await client.query(prompt)`,
`async for msg in client.receive_response()` yields `AssistantMessage(content=[TextBlock|ToolUseBlock|…])`,
`UserMessage` (tool results), `SystemMessage`, `ResultMessage(session_id, result, total_cost_usd, is_error, num_turns)`;
`await client.interrupt()`, `await client.disconnect()`. `client.query()` may be called again to send a follow-up.
**Tool restriction (ruling 2026-08-18, superseded 2026-08-24):** under `permission_mode="bypassPermissions"`
the `allowed_tools` option is only an auto-approve list and restricts nothing; the enforcing option is
`tools=[…]`. That still holds — but nothing is restricted any more: `tools` is never set, so every subagent
keeps the full built-in set (skills and its own subagents included), and the google MCP server is attached to
every task with `allowed_tools=["mcp__google__*"]` for the MCP wildcard.

**Google Workspace MCP** — `uvx workspace-mcp --tools gmail calendar --transport stdio --single-user`;
env `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/oauth2callback`,
`USER_GOOGLE_EMAIL`, `GOOGLE_MCP_CREDENTIALS_DIR`, `OAUTHLIB_INSECURE_TRANSPORT=1`. Wire as
`mcp_servers={"google":{"type":"stdio","command":"uvx","args":[…],"env":{…}}}`, `allowed_tools=["mcp__google__*"]`.

**sounddevice 0.5.x** — `InputStream(samplerate=24000, dtype="int16", channels=1, blocksize=1920, callback=…)`
(80 ms frames → soxr → 1280 samples @16 kHz for the wake word), `RawOutputStream(samplerate=24000, dtype="int16", channels=1, callback=…)`.
Callbacks run on the PortAudio thread → hand off with `loop.call_soon_threadsafe`.

## 5. Security model

`bypassPermissions` = the subagents have William's full user access, and since the kinds
collapsed (2026-08-24) that includes Gmail and Calendar on every task — which is why the phone
PIN now gates every dispatch rather than two of four kinds. Exposure surface: the
Cloudflare tunnel to `/twilio/*` (signature-validated + allowlist + one-time stream token) and
`/reports/{id}?t=` (HMAC token). PIN protects destructive task kinds on the phone channel.
Caller ID is spoofable → the PIN is the real gate for `coding`/`cowork`.
